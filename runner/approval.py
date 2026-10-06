"""Approval gate for mutating model tool calls.

No ``AGENTS_APPROVAL_CMD`` means today's behavior: tools run immediately.
When the command is set, ``AGENTS_APPROVAL_MODE`` selects the gate:

- ``off``: never ask
- ``ask``: mutators only (default when a command is set and the mode is empty)
- ``strict``: every tool that is not read-only

The command gets one JSON object on stdin and exits 0 (approved), 1 (denied),
or 2 (timeout / unavailable, treated as denied). ``{user}`` in the command is
replaced with the turn's channel user before the process starts.

Denied results are harness instructions. They are not untrusted tool data, and
they tell the model not to retry the same call.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from typing import Any

_READ_TAILS = {
    "search",
    "list",
    "read",
    "show",
    "stats",
    "related",
    "projects",
    "catalog",
    "load",
    "check",
    "audit",
}
_MUTATE_TAILS = {
    "write",
    "add",
    "remove",
    "delete",
    "terminal",
    "seal",
    "ingest",
    "update",
    "create",
    "set",
    "put",
    "patch",
}


def approval_command() -> str:
    return os.environ.get("AGENTS_APPROVAL_CMD", "").strip()


def approval_mode() -> str:
    """Resolved mode. Empty mode + command => ask. Empty mode + no command => off."""
    mode = os.environ.get("AGENTS_APPROVAL_MODE", "").strip().lower()
    if mode in ("off", "ask", "strict"):
        return mode
    if approval_command():
        return "ask"
    return "off"


def _tail(name: str) -> str:
    return str(name or "").strip().lower().rsplit(".", 1)[-1]


def _read_only_hint(mod: dict[str, Any]) -> bool:
    if mod.get("read_only") is True or mod.get("readOnlyHint") is True:
        return True
    ann = mod.get("annotations")
    return isinstance(ann, dict) and ann.get("readOnlyHint") is True


def module_is_read_only(mod: dict[str, Any]) -> bool:
    """Explicit ``mutates: false`` and readOnlyHint win. Remote tools are not read-only otherwise."""
    if "mutates" in mod:
        return not bool(mod.get("mutates"))
    if _read_only_hint(mod):
        return True
    if str(mod.get("kind") or "") == "mcp_remote":
        return False
    return _tail(str(mod.get("name") or "")) in _READ_TAILS


def module_is_mutator(mod: dict[str, Any]) -> bool:
    """Manifest ``mutates`` wins. Remote MCP tools mutate unless readOnlyHint is true."""
    if "mutates" in mod:
        return bool(mod.get("mutates"))
    if str(mod.get("kind") or "") == "mcp_remote":
        return not _read_only_hint(mod)
    if _read_only_hint(mod):
        return False
    tail = _tail(str(mod.get("name") or ""))
    if tail in _READ_TAILS:
        return False
    if tail in _MUTATE_TAILS:
        return True
    blob = str(mod.get("name") or "").lower().replace(".", "_")
    return any(tok in blob.split("_") for tok in _MUTATE_TAILS)


def needs_approval(mod: dict[str, Any]) -> bool:
    mode = approval_mode()
    if mode == "off" or not approval_command():
        return False
    if mode == "strict":
        return not module_is_read_only(mod)
    return module_is_mutator(mod)


def summarize(tool: str, args: Any) -> str:
    if isinstance(args, dict):
        bits: list[str] = []
        for key, val in list(args.items())[:6]:
            text = str(val).replace("\n", " ")
            if len(text) > 80:
                text = text[:77] + "..."
            bits.append(f"{key}={text}")
        detail = ", ".join(bits)
    else:
        detail = str(args).replace("\n", " ")[:160]
    line = f"{tool} {detail}".strip()
    return line[:240]


def _command_argv(user: str) -> list[str]:
    parts = shlex.split(approval_command(), posix=True)
    return [part.replace("{user}", user) for part in parts]


def _timeout_sec(argv: list[str]) -> float:
    env = os.environ.get("AGENTS_APPROVAL_TIMEOUT", "").strip()
    if env:
        try:
            return max(1.0, float(env))
        except ValueError:
            pass
    if "--timeout" in argv:
        idx = argv.index("--timeout")
        if idx + 1 < len(argv):
            try:
                return max(1.0, float(argv[idx + 1]) + 15.0)
            except ValueError:
                pass
    return 330.0


def _note_from_stdout(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if isinstance(data, dict) and data.get("note"):
        return str(data["note"]).strip()
    return ""


def _jsonable(args: Any) -> Any:
    if isinstance(args, (dict, list, str, int, float, bool)) or args is None:
        return args
    return str(args)


def _denied(req_id: str, tool: str, why: str, note: str = "") -> str:
    msg = f"Denied: {why} (id={req_id}, tool={tool}). Do not retry the same tool call."
    if note:
        msg += f" Note: {note}"
    return msg


def request_approval(
    *,
    tool: str,
    args: Any,
    session: str = "",
    user: str = "",
    summary: str = "",
) -> tuple[bool, str]:
    """Run the approval command. ``(True, "")`` means proceed."""
    req_id = str(uuid.uuid4())
    payload = {
        "id": req_id,
        "session": session or "",
        "user": user or "",
        "tool": tool,
        "args": _jsonable(args),
        "summary": summary or summarize(tool, args),
    }
    argv = _command_argv(user or "")
    if not argv:
        return False, _denied(req_id, tool, "approval command is empty")
    try:
        proc = subprocess.run(
            argv,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=_timeout_sec(argv),
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return False, _denied(req_id, tool, "approval timed out or was unavailable")
    except OSError as exc:
        return False, _denied(
            req_id, tool, "approval timed out or was unavailable", note=str(exc)
        )
    note = _note_from_stdout(proc.stdout or "")
    if proc.returncode == 0:
        return True, ""
    if proc.returncode == 2:
        why = "approval timed out or was unavailable"
    else:
        why = "the user declined this tool call"
    return False, _denied(req_id, tool, why, note)


def gate_module(
    mod: dict[str, Any],
    args: Any,
    *,
    session: str = "",
    user: str = "",
) -> tuple[bool, str]:
    """Return ``(allowed, denial_message)``. Skipped gates are allowed."""
    if not needs_approval(mod):
        return True, ""
    tool = str(mod.get("name") or "")
    payload = args if isinstance(args, dict) else {"arguments": args}
    return request_approval(
        tool=tool,
        args=payload,
        session=session,
        user=user,
        summary=summarize(tool, args),
    )
