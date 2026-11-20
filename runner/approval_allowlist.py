"""Persistent approval allowlist.

When the approval command approves a call and asks to remember it — stdout
JSON ``{"remember": true}`` or an explicit ``{"allow_pattern": "<fnmatch>"}`` —
the pattern lands in the allowlist file. Later calls that match skip the
prompt. ``deny`` rules still win; the allowlist only removes an ``ask``
prompt, never a policy block.

File: ``AGENTS_APPROVAL_ALLOWLIST`` when set (``0|off|none|false`` disables),
else ``<host>/approval-allowlist.json``. Body: ``{"patterns": ["<fnmatch>",
...]}`` or a bare list. A pattern is matched (``fnmatch``) against two renderings
of the call: the canonical ``<tool> <args-json>`` signature and the
human-readable ``summarize`` line, so ``mcp.terminal *git status*``-style
entries work.
"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
from typing import Any

_OFF = ("0", "off", "none", "false", "no")


def store_path(workspace: Any = None) -> Path | None:
    raw = os.environ.get("AGENTS_APPROVAL_ALLOWLIST", "").strip()
    if raw:
        if raw.lower() in _OFF:
            return None
        return Path(raw).expanduser()
    host = getattr(workspace, "host", None)
    if host is None:
        try:
            from agents_workspace import Host

            host = Host.from_env()
        except Exception:
            return None
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
