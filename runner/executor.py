"""agents-harness — Declarative Scheduled Flow Executor

Strictly layered, stdlib-only runner for local agent services.
Reads declarative manifests from runner/schedules/*.json, invokes verified CLIs,
records exit codes, and appends structured JSONL logs.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .redact import redact_tool_output

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCHEDULES_DIR = Path(__file__).parent / "schedules"
DEFAULT_LOG_FILE = Path(__file__).parent.parent / "logs" / "runner.jsonl"


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
        "stdout_tail": stdout_text[-1000:] if stdout_text else "",
        "stderr_tail": stderr_text[-1000:] if stderr_text else "",
        "extra_argv": extra_argv or [],
    }

    # Local repo log, plus user-agent traces when enabled (env or existing dir)
    append_log(record, log_path)
    if traces_logging_enabled():
        append_log(record, traces_dir() / "runner.jsonl")

    return record


def append_log(record: Dict[str, Any], path: Optional[Path]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
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
