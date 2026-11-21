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
        ws_dir_fn = getattr(host, "_workspaces_dir", None)
        if callable(ws_dir_fn):
            try:
                roots.append(Path(ws_dir_fn()))
            except Exception:
                pass
        ws_parent = getattr(host, "workspaces_dir", None)
        if ws_parent is not None:
            roots.append(Path(ws_parent))
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


def load_patterns(workspace: Any = None) -> list[str]:
    path = store_path(workspace)
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


def remember(tool: str, args: Any, pattern: str = "", *, workspace: Any = None) -> str | None:
    """Persist ``pattern`` (default: the exact signature). Returns it, or None."""
    path = store_path(workspace)
    if path is None:
        return None
    pat = (pattern or "").strip() or _canonical(tool, args)
    patterns = load_patterns(workspace)
    if pat not in patterns:
        patterns.append(pat)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"patterns": patterns}, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            return None
    return pat
