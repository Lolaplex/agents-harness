"""agents-harness — Declarative Scheduled Flow Executor

Strictly layered, stdlib-only runner for local agent services.
Reads declarative manifests from runner/schedules/*.json, invokes verified CLIs,
records exit codes, and appends structured JSONL logs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .redact import redact_tool_output

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCHEDULES_DIR = Path(__file__).parent / "schedules"


def _default_log_file() -> Optional[Path]:
    repo_root = Path(__file__).parent.parent
    if (repo_root / ".git").is_dir():
        return repo_root / "logs" / "runner.jsonl"
    return None


DEFAULT_LOG_FILE = _default_log_file()

# Trace records stay small. Model- and user-visible text uses a larger cap.
TRACE_TAIL_CHARS = 1000
DEFAULT_TOOL_OUTPUT_MAX = 32768


def tool_output_max() -> int:
    """Character cap for tool text shown to a model or a schedule caller.

    ``AGENTS_TOOL_OUTPUT_MAX`` overrides the default. A non-integer or a
    negative value falls back to the default. Zero is a real cap.
    """
    raw = os.environ.get("AGENTS_TOOL_OUTPUT_MAX", "").strip()
    if not raw:
        return DEFAULT_TOOL_OUTPUT_MAX
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TOOL_OUTPUT_MAX
    if value < 0:
        return DEFAULT_TOOL_OUTPUT_MAX
    return value


def clip_tool_output(text: str, limit: int | None = None) -> str:
    """Return ``text`` unchanged when it fits, else head + marker + tail.

    The marker is ``[... N chars omitted ...]`` with ``N`` the exact number of
    characters removed from the middle. The result is at most ``limit``
    characters when the cap can hold the marker.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    cap = tool_output_max() if limit is None else limit
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = DEFAULT_TOOL_OUTPUT_MAX
    if cap < 0:
        cap = DEFAULT_TOOL_OUTPUT_MAX
    if len(text) <= cap:
        return text
    if cap == 0:
        return ""

    def _marker(omitted: int) -> str:
        return f"[... {omitted} chars omitted ...]"

    omitted = max(1, len(text) - cap)
    for _ in range(8):
        keep = cap - len(_marker(omitted))
        if keep < 0:
            keep = 0
        next_omitted = len(text) - keep
        if next_omitted < 1:
            next_omitted = len(text)
        if next_omitted == omitted:
            break
        omitted = next_omitted

    marker = _marker(omitted)
    if len(marker) > cap:
        return marker[:cap]

    keep = cap - len(marker)
    head_len = keep // 2
    tail_len = keep - head_len
    omitted = len(text) - head_len - tail_len
    marker = _marker(omitted)
    while head_len + tail_len + len(marker) > cap and head_len + tail_len > 0:
        if tail_len > head_len and tail_len > 0:
            tail_len -= 1
        elif head_len > 0:
            head_len -= 1
        else:
            tail_len -= 1
        omitted = len(text) - head_len - tail_len
        marker = _marker(max(omitted, 0))

    head = text[:head_len]
    tail = text[-tail_len:] if tail_len else ""
    return head + marker + tail


def present_job_result(record: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a job record for a model or a user.

    Full ``stdout`` and ``stderr`` are not included. Those strings are clipped
    into the tail fields. A clipped field keeps a ``<ref>`` so ``read_content``
    can load the full text. The trace record written by ``execute_job`` is
    separate and still holds only the short tail.
    """
    from .retrieve import annotate_clip

    out = dict(record)
    stdout = out.pop("stdout", None)
    stderr = out.pop("stderr", None)
    if isinstance(stdout, str):
        out["stdout_tail"] = annotate_clip(stdout)
    if isinstance(stderr, str):
        out["stderr_tail"] = annotate_clip(stderr)
    return out


def _trace_tail(text: str) -> str:
    if not text:
        return ""
    return text[-TRACE_TAIL_CHARS:]


def _schedule_dirs() -> list[Path]:
    dirs: list[Path] = []
    bundled = os.environ.get("AGENTS_BUNDLED_SCHEDULES", "1").strip().lower()
    if bundled not in ("0", "false", "no", "off"):
        dirs.append(SCHEDULES_DIR)
    extra = os.environ.get("AGENTS_SCHEDULES_DIR", "").strip()
    if extra:
        overlay = Path(extra)
        if overlay.resolve() not in {d.resolve() for d in dirs}:
            dirs.append(overlay)
    return dirs


def list_schedules(schedules_dir: Path | None = None) -> List[Dict[str, Any]]:
    if schedules_dir is not None:
        dirs = [schedules_dir]
    else:
        dirs = _schedule_dirs()
    seen: dict[str, Dict[str, Any]] = {}
    for folder in dirs:
        if not folder.exists():
            continue
        for p in sorted(folder.glob("*.json")):
            if p.name in ("tick-state.json", "enabled.json"):
                continue
            try:
                row = load_manifest(p)
                seen[str(row["name"])] = row
            except Exception as e:
                print(f"[!] Error loading {p.name}: {e}", file=sys.stderr)
    return list(seen.values())


def traces_dir() -> Path:
    """Cordis job-log directory. Same override as agents-traces.

    AGENTS_TRACES_DIR wins. Otherwise ~/.agents/traces. Read at call time so
    a sandbox env cannot leak into a module-level Path.home() capture.
    agents-traces does not map AGENTS_HOME onto traces, so neither do we.
    """
    env_dir = os.environ.get("AGENTS_TRACES_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return Path.home() / ".agents" / "traces"


def traces_logging_enabled() -> bool:
    """Match agents-traces interceptor: env set, or default dir already exists."""
    if os.environ.get("AGENTS_TRACES_DIR", "").strip():
        return True
    return traces_dir().is_dir()


def verb_to_argv(verb: str, extra_argv: List[str] | None = None) -> List[str]:
    """Parse a Cordis verb into an argv list. Never uses a shell."""
    parts = shlex.split(str(verb), posix=True)
    if parts and parts[0] in ("python", "python3", "py"):
        parts[0] = sys.executable
    if extra_argv:
        parts.extend(str(a) for a in extra_argv)
    return parts


def load_manifest(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for required in ("name", "verb", "cadence", "rests_on"):
        if required not in data:
            raise ValueError(f"Manifest {path.name} missing required field: '{required}'")
    data.setdefault("expected_exit", 0)
    data.setdefault("timeout_sec", 60)
    data.setdefault("cwd", None)
    data["_source_file"] = str(path)
    return data


_ESCAPE = "path escapes workspace root"
_WIN_ABS = re.compile(r"[A-Za-z]:[\\/][^\"'\s),;]+")
_UNC = re.compile(r"\\\\[^\"'\s),;]+")
_EMBED_REL = re.compile(r"(?:^|[\s\"'=])(\.\.[\\/][^\"'\s),;]*)")
_POSIX_ABS = re.compile(r"(?:^|[\s\"'=])(/[^\"'\s),;]+)")


def workspace_root() -> Path | None:
    """Resolved ``AGENTS_WORKSPACE_DIR``, or None when the lock is off."""
    raw = os.environ.get("AGENTS_WORKSPACE_DIR", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path


def _path_key(path: Path) -> str:
    try:
        text = os.path.normcase(os.path.normpath(str(path.expanduser().resolve(strict=False))))
    except (OSError, RuntimeError):
        return ""
    for prefix in ("\\\\?\\unc\\", "\\\\?\\"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text


def _under(root: Path, path: Path) -> bool:
    root_s = _path_key(root)
    child_s = _path_key(path)
    if not root_s or not child_s:
        return False
    if child_s == root_s:
        return True
    prefix = root_s if root_s.endswith(os.sep) else root_s + os.sep
    return child_s.startswith(prefix)


def _agents_home() -> Path:
    raw = os.environ.get("AGENTS_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".agents"


def _internal_roots() -> list[Path]:
    """Dirs the workspace lock must not treat as escapes.

    ``AGENTS_HOME`` is the instance root when a host sets it, otherwise
    ``~/.agents``. Skills, modules, memory, traces, and schedules stay
    reachable even when that home is a narrower instance directory.
    """
    home = _agents_home()
    dot = Path.home() / ".agents"
    roots = [
        home,
        dot / "skills",
        dot / "modules",
        dot / "memory",
        dot / "traces",
        dot / "schedules",
        home / "skills",
        home / "modules",
        home / "memory",
        home / "traces",
        home / "schedules",
        home / "routine-runs",
    ]
    for key in (
        "AGENTS_SKILLS_DIR",
        "AGENTS_MODULES_DIR",
        "AGENTS_MEMORY_PATH",
        "AGENTS_TRACES_DIR",
        "AGENTS_SCHEDULES_DIR",
        "AGENTS_ROUTINE_RUNS_DIR",
    ):
        raw = os.environ.get(key, "").strip()
        if raw:
            roots.append(Path(raw).expanduser())
    extra = os.environ.get("AGENTS_SKILLS_EXTRA", "")
    for part in extra.split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(Path(part).expanduser())
    return roots


def _permitted(path: Path, root: Path) -> bool:
    if _under(root, path):
        return True
    for base in _internal_roots():
        if _under(base, path):
            return True
    return False


def _pieces(text: str) -> list[str]:
    if text.startswith("-"):
        eq = text.find("=")
        return [] if eq < 0 else _pieces(text[eq + 1 :])
    raw = Path(text).expanduser()
    if raw.is_absolute() or ".." in raw.parts:
        return [text]
    if not any(ch.isspace() for ch in text):
        return [text]
    if (
        _WIN_ABS.search(text)
        or _UNC.search(text)
        or _EMBED_REL.search(text)
        or (os.name != "nt" and _POSIX_ABS.search(text))
    ):
        try:
            return [part for part in shlex.split(text, posix=(os.name != "nt")) if part]
        except ValueError:
            return text.split()
    return [text]


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return True


def _piece_paths(piece: str, base: Path) -> list[Path]:
    if not piece or piece == "." or "://" in piece:
        return []
    if os.name == "nt" and re.fullmatch(r"/[A-Za-z?][\w.-]*", piece):
        return []
    raw = Path(piece).expanduser()
    if raw.is_absolute() or ".." in raw.parts:
        return [raw if raw.is_absolute() else base / raw]
    if "/" in piece or "\\" in piece:
        return [base / raw]
    candidate = base / raw
    try:
        linked = candidate.is_symlink()
        exists = candidate.exists()
    except OSError:
        return [candidate]
    if linked or exists:
        return [candidate]
    return []


def _argv_paths(argv: Sequence[str], base: Path) -> list[Path]:
    found: list[Path] = []
    for index, arg in enumerate(argv):
        text = str(arg).strip()
        if not text:
            continue
        if index == 0:
            raw = Path(text).expanduser()
            if not raw.is_absolute() and (
                ".." in raw.parts or "/" in text or "\\" in text or _is_symlink(base / raw)
            ):
                found.append(base / raw)
            continue
        for piece in _pieces(text):
            found.extend(_piece_paths(piece, base))
        for match in _WIN_ABS.findall(text):
            found.append(Path(match))
        for match in _UNC.findall(text):
            found.append(Path(match))
        for match in _EMBED_REL.findall(text):
            found.append(base / match)
        if os.name != "nt":
            for match in _POSIX_ABS.findall(text):
                found.append(Path(match))
    return found


def refuse_workspace(cwd: str | Path | None, argv: Sequence[str] = ()) -> str | None:
    """Refuse a user cwd or tool path that resolves outside ``AGENTS_WORKSPACE_DIR``.

    Unset or blank env keeps the current behavior. Skills, modules, memory,
    traces, schedules, and ``AGENTS_HOME`` stay reachable. Symlink targets count.
    """
    root = workspace_root()
    if root is None:
        return None
    explicit: Path | None = None
    if cwd is not None and str(cwd).strip():
        explicit = Path(str(cwd)).expanduser()
        if not _permitted(explicit, root):
            return _ESCAPE
    base = explicit if explicit is not None else Path.cwd()
    try:
        base = base.resolve(strict=False)
    except (OSError, RuntimeError):
        return _ESCAPE
    for path in _argv_paths(argv, base):
        if not _permitted(path, root):
            return _ESCAPE
    return None


def execute_job(
    manifest: Dict[str, Any],
    log_path: Optional[Path] = DEFAULT_LOG_FILE,
    extra_argv: Optional[List[str]] = None,
) -> Dict[str, Any]:
    name = manifest["name"]
    verb = manifest["verb"]
    expected_exit = manifest.get("expected_exit", 0)
    timeout_sec = manifest.get("timeout_sec", 60)
    cwd = manifest.get("cwd")
    extra_argv = [str(a) for a in extra_argv] if extra_argv else None

    start_time = time.time()
    iso_timestamp = datetime.now(timezone.utc).isoformat()

    print(f"[*] Running '{name}' (rests on: {manifest['rests_on']})...", file=sys.stderr)
    print(f"    CMD: {verb}" + (f" {extra_argv}" if extra_argv else ""), file=sys.stderr)

    status = "SUCCESS"
    exit_code = -1
    stdout_text = ""
    stderr_text = ""

    try:
        cmd = verb_to_argv(verb, extra_argv)
        refusal = refuse_workspace(cwd, cmd)
        if refusal:
            status = "ERROR"
            exit_code = 1
            stderr_text = refusal
            print(f"[-] '{name}' ERROR: {refusal}", file=sys.stderr)
        else:
            proc = subprocess.run(
                cmd,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                cwd=cwd,
            )
            exit_code = proc.returncode
            stdout_text = redact_tool_output(proc.stdout.strip())
            stderr_text = proc.stderr.strip()

            if exit_code != expected_exit:
                status = "FAILED"
                print(f"[-] '{name}' FAILED: expected exit {expected_exit}, got {exit_code}", file=sys.stderr)
                if stderr_text:
                    print(f"    STDERR: {stderr_text[:500]}", file=sys.stderr)
            else:
                print(
                    f"[+] '{name}' OK (exit {exit_code}) in {time.time() - start_time:.2f}s",
                    file=sys.stderr,
                )

    except subprocess.TimeoutExpired:
        status = "TIMEOUT"
        print(f"[-] '{name}' TIMEOUT after {timeout_sec}s", file=sys.stderr)
    except Exception as e:
        status = "ERROR"
        stderr_text = str(e)
        print(f"[-] '{name}' ERROR: {e}", file=sys.stderr)

    duration_ms = (time.time() - start_time) * 1000.0
    record = {
        "timestamp": iso_timestamp,
        "job": name,
        "verb": verb,
        "rests_on": manifest["rests_on"],
        "cadence": manifest["cadence"],
        "status": status,
        "exit_code": exit_code,
        "expected_exit": expected_exit,
        "duration_ms": round(duration_ms, 2),
        "stdout_tail": _trace_tail(stdout_text),
        "stderr_tail": _trace_tail(stderr_text),
        "extra_argv": extra_argv or [],
    }

    # Local repo log, plus user-agent traces when enabled (env or existing dir).
    # The log keeps the short tail only. Callers receive the full strings too.
    append_log(record, log_path)
    if traces_logging_enabled():
        append_log(record, traces_dir() / "runner.jsonl")

    returned = dict(record)
    returned["stdout"] = stdout_text
    returned["stderr"] = stderr_text
    return returned


def append_log(record: Dict[str, Any], path: Optional[Path]) -> None:
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    try:
        from . import __version__
        from .updates import check_for_updates

        check_for_updates("agents-harness", __version__)
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="agents-harness declarative scheduled flow runner")
    parser.add_argument("--list", action="store_true", help="List all discovered schedule manifests")
    parser.add_argument("--validate", action="store_true", help="Validate manifest syntax and requirements")
    parser.add_argument("--all", action="store_true", help="Run all scheduled flows sequentially")
    parser.add_argument("--run", type=str, help="Run a specific flow by name")
    args = parser.parse_args(argv)

    schedules = list_schedules()

    if args.list:
        print(f"Found {len(schedules)} schedule manifests in {SCHEDULES_DIR}:")
        for s in schedules:
            print(f"  - {s['name']:<24} [{s['cadence']:<8}] rests on: {s['rests_on']}")
        return 0

    if args.validate:
        print(f"Validating {len(schedules)} schedule manifests...")
        for s in schedules:
            print(f"  [+] {s['name']}: VALID (verb: '{s['verb']}')")
        return 0

    if args.run:
        match = [s for s in schedules if s["name"].lower() == args.run.lower()]
        if not match:
            print(f"Error: Job '{args.run}' not found.", file=sys.stderr)
            return 1
        res = execute_job(match[0])
        return 0 if res["status"] == "SUCCESS" else 1

    if args.all or len(sys.argv) == 1:
        failed = 0
        for s in schedules:
            res = execute_job(s)
            if res["status"] != "SUCCESS":
                failed += 1
        return 1 if failed > 0 else 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
