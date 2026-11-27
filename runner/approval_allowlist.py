"""Persistent approval allowlist.

When the approval command approves a call and asks to remember it — stdout
JSON ``{"remember": true}`` or an explicit ``{"allow_pattern": "<fnmatch>"}`` —
the pattern lands in the allowlist file. Later calls that match skip the
prompt. ``deny`` rules still win; the allowlist only removes an ``ask``
prompt, never a policy block.

File: ``AGENTS_APPROVAL_ALLOWLIST`` when set (``0|off|none|false`` disables;
paths inside a workspace or workdir are rejected), else
``Host.from_env().approval_allowlist`` (``<host>/approval-allowlist.json``).
Body: ``{"patterns": ["<fnmatch>", ...]}`` or a bare list. A pattern is matched
(``fnmatch``) against two renderings of the call: the canonical
``<tool> <args-json>`` signature and the human-readable ``summarize`` line, so
``mcp.terminal *git status*``-style entries work.
"""

from __future__ import annotations

import fnmatch
import json
import os
import warnings
from pathlib import Path
from typing import Any

_OFF = ("0", "off", "none", "false", "no")


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


def _fallback_host_dir() -> Path:
    raw = os.environ.get("AGENTS_HOST_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".agents"


def _resolve_host(workspace: Any = None) -> tuple[Any | None, bool]:
    """Return ``(host_obj, import_failed)`` without creating a workspace."""
    host = getattr(workspace, "host", None)
    if host is not None:
        return host, False
    try:
        from agents_workspace import Host

        return Host.from_env(), False
    except ImportError:
        return None, True
    except Exception:
        return None, False


def _workspace_roots(workspace: Any = None, host: Any | None = None) -> list[Path]:
    """Workspace and workdir roots that must never hold the allowlist file."""
    roots: list[Path] = []
    host_dir = getattr(host, "dir", None) if host is not None else _fallback_host_dir()
    host_key = _path_key(Path(host_dir)) if host_dir is not None else ""

    if host is not None:
        ws_parent = getattr(host, "workspaces_dir", None)
        if ws_parent is not None:
            roots.append(Path(ws_parent))
        elif host_dir is not None:
            roots.append(Path(host_dir) / "workspaces")
    elif host_dir is not None:
        roots.append(Path(host_dir) / "workspaces")

    if workspace is not None:
        ws_dir = getattr(workspace, "dir", None)
        if ws_dir is not None:
            ws_path = Path(ws_dir)
            if not host_key or _path_key(ws_path) != host_key:
                roots.append(ws_path)
        ws_workdir = getattr(workspace, "workdir", None)
        if ws_workdir is not None:
            roots.append(Path(ws_workdir))

    for var in (
        "AGENTS_WORKSPACES_DIR",
        "AGENTS_WORKSPACE_ROOT",
        "AGENTS_WORKDIR",
        "AGENTS_WORKSPACE_DIR",
    ):
        raw = os.environ.get(var, "").strip()
        if raw:
            roots.append(Path(raw).expanduser())
    return roots


def _is_workspace_path(candidate: Path, workspace: Any = None, host: Any | None = None) -> bool:
    if not _path_key(candidate):
        return True
    return any(_under(root, candidate) for root in _workspace_roots(workspace, host))


def store_path(workspace: Any = None) -> Path | None:
    host, import_failed = _resolve_host(workspace)
    raw = os.environ.get("AGENTS_APPROVAL_ALLOWLIST", "").strip()
    if raw:
        if raw.lower() in _OFF:
            return None
        candidate = Path(raw).expanduser()
        if _is_workspace_path(candidate, workspace, host):
            warnings.warn(
                f"AGENTS_APPROVAL_ALLOWLIST inside workspace is rejected: {raw}",
                UserWarning,
                stacklevel=2,
            )
            return None
        return candidate
    if import_failed:
        return _fallback_host_dir() / "approval-allowlist.json"
    if host is None:
        return None
    ws = workspace
    if ws is None:
        try:
            from .mcp_client import _resolve_ws_no_create

            ws = _resolve_ws_no_create()
        except Exception:
            ws = None
    host_dir = getattr(host, "dir", None)
    if ws is not None and host_dir is not None:
        ws_id = str(getattr(ws, "id", "") or "").strip()
        # A legacy host resolves as its own workspace with the host dir name
        # as a phantom id. There is no per-workspace trust boundary on an
        # unmigrated host — and the pre-upgrade host-wide allowlist must keep
        # applying — so only registered workspaces get a scoped store.
        ws_dir = getattr(ws, "dir", None)
        is_legacy_host = ws_dir is not None and Path(ws_dir) == Path(host_dir)
        if ws_id and not is_legacy_host:
            return Path(host_dir) / "approvals" / f"ws-{ws_id}.allowlist.json"
    allowlist = getattr(host, "approval_allowlist", None)
    if allowlist is not None:
        return Path(allowlist)
    host_dir = getattr(host, "dir", None)
    if host_dir is not None:
        return Path(host_dir) / "approval-allowlist.json"
    agents = getattr(host, "agents_json", None)
    if agents is None:
        return None
    return Path(agents).parent / "approval-allowlist.json"


def _read_patterns(path: Path | None) -> list[str]:
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("patterns")
    if not isinstance(data, list):
        return []
    return [str(p).strip() for p in data if str(p).strip()]


def load_patterns(workspace: Any = None) -> list[str]:
    path = store_path(workspace)
    patterns = _read_patterns(path)
    # Migration read: a workspace-scoped path never loses patterns that only
    # exist in the legacy host-wide file.
    if path is not None and path.name != "approval-allowlist.json":
        host, _ = _resolve_host(workspace)
        host_dir = getattr(host, "dir", None) if host is not None else None
        legacy = Path(host_dir) / "approval-allowlist.json" if host_dir else None
        patterns += [p for p in _read_patterns(legacy) if p not in patterns]
    return patterns


def _canonical(tool: str, args: Any) -> str:
    payload = args if isinstance(args, dict) else {"arguments": args}
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return f"{tool} {body}"


def _flat(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        for v in value.values():
            _flat(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _flat(v, out)
    elif value is not None:
        out.append(str(value))


def _targets(tool: str, args: Any) -> list[str]:
    from .approval import summarize

    flat_parts: list[str] = []
    _flat(args, flat_parts)
    return [_canonical(tool, args), summarize(tool, args), f"{tool} {' '.join(flat_parts)}".rstrip()]


def is_allowed(tool: str, args: Any, *, workspace: Any = None) -> bool:
    patterns = load_patterns(workspace)
    if not patterns:
        return False
    targets = _targets(tool, args)
    return any(
        target == pattern or fnmatch.fnmatchcase(target, pattern)
        for pattern in patterns
        for target in targets
    )


def _write_patterns(path: Path, patterns: list[str]) -> bool:
    import os
    import tempfile

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".allowlist_", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"patterns": patterns}, indent=2, ensure_ascii=False))
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return False
    return True


def remember(tool: str, args: Any, pattern: str = "", *, workspace: Any = None) -> str | None:
    """Persist ``pattern`` (default: the exact signature). Returns it, or None."""
    path = store_path(workspace)
    if path is None:
        return None
    pat = (pattern or "").strip() or _canonical(tool, args)
    patterns = _read_patterns(path)
    if pat not in patterns:
        patterns.append(pat)
        if not _write_patterns(path, patterns):
            return None
    return pat


def remove_pattern(pattern: str, *, workspace: Any = None) -> bool:
    """Remove ``pattern`` from workspace and/or host allowlist. Returns True if removed."""
    pat = (pattern or "").strip()
    if not pat:
        return False
    path = store_path(workspace)
    if path is None:
        return False
    candidates: list[Path] = [path]
    if path.name != "approval-allowlist.json":
        host, _ = _resolve_host(workspace)
        host_dir = getattr(host, "dir", None) if host is not None else None
        if host_dir:
            legacy = Path(host_dir) / "approval-allowlist.json"
            if legacy != path:
                candidates.append(legacy)
    removed = False
    for target in candidates:
        existing = _read_patterns(target)
        if pat in existing:
            updated = [p for p in existing if p != pat]
            if _write_patterns(target, updated):
                removed = True
    return removed
