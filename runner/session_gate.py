"""Session bind gate (klanker#145 R6 / harness#38, hardened by harness#42).

The first HOST-LOCAL tool call in a loop process marks the session as
executing on this host. The harness owns no bind semantics — it fires
``AGENTS_SESSION_BIND_CMD`` once per process, formatted with
``{session}``, ``{tool}`` and ``{workspace}`` placeholders. The host app
(klanker) maps that to a ``session:host`` datom; without the env var
the gate is a no-op.

Host-local = tools that touch this host's filesystem, processes or
devices: the canonical module name in ``HOST_LOCAL_TOOLS`` (or under a
dotted prefix), a manifest/command spec flagged ``host_local: true``, or
a stdio ``mcp_remote`` server (an arbitrary local binary). ws-data tools
(memory/calendar/traces/persona/schedule/skills) and HTTP/SSE remotes
stay portable and never bind.

Callers resolve the canonical module BEFORE this gate — the checked
name is always ``mod["name"]``, never the raw LLM tool string. The gate
runs after approval and before the tool executes, and it fails closed:
``SessionGateError`` aborts the call instead of running unbound and does
not latch ``_fired``. A child turn spawned with ``AGENTS_PARENT_SESSION``
set binds that session, so delegate/persona children map the root turn
to this host, never ``ses_dlg_*``.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Canonical names that bind this host: exact names plus dotted prefixes.
_HOST_LOCAL_TOKENS = (
    "git", "git.", "mcp.terminal", "mcp.terminal.", "mcp.browser",
    "mcp.browser.", "mcp.delegate", "mcp.delegate.", "mcp.fs",
    "mcp.proc", "mcp.process", "mcp.voice", "mcp.voice.", "mcp.keys",
    "mcp.keys.", "mcp.attest", "mcp.attest.", "fs", "fs.", "proc",
    "proc.", "process", "terminal", "terminal.", "workdir",
    "repo.pre_pr", "vision.analyze",
)
HOST_LOCAL_TOOLS = frozenset(t for t in _HOST_LOCAL_TOKENS if not t.endswith("."))
_HOST_LOCAL_PREFIXES = tuple(t for t in _HOST_LOCAL_TOKENS if t.endswith("."))

_fired = False
_gate_fn = None


class SessionGateError(Exception):
    """A configured bind failed; the tool must not run (fail closed)."""


def register_session_gate(fn):
    """In-process bind hook (called once per process, first host-local
    tool). Preferred over the env cmd when the host registers one —
    e.g. klanker.turn writes ``session:host`` directly into its store.
    The hook may return a bool or a dict; ``{"bound": true, "workdir":
    "...", "already_bound": bool}`` applies the session worktree and a
    decline (``False`` / ``{"bound": false}``) raises SessionGateError."""
    global _gate_fn
    _gate_fn = fn
    return fn


def clear_session_gate() -> None:
    global _gate_fn
    _gate_fn = None


def _manifest_host_local(mod: Any) -> bool:
    if not isinstance(mod, dict):
        return False
    if mod.get("host_local"):
        return True
    commands = mod.get("commands")
    if not isinstance(commands, dict):
        return False
    pin = str(mod.get("command_pin") or "").strip()
    if pin:
        spec = commands.get(pin)
        return bool(isinstance(spec, dict) and spec.get("host_local"))
    return any(
        isinstance(spec, dict) and spec.get("host_local")
        for spec in commands.values()
    )


def _stdio_remote(mod: Any) -> bool:
    """kind ``mcp_remote`` on stdio transport = an arbitrary local binary."""
    if not isinstance(mod, dict) or str(mod.get("kind") or "") != "mcp_remote":
        return False
    if mod.get("command"):
        return True
    transport = str(mod.get("transport") or "").strip().lower()
    if transport:
        return transport == "stdio"
    server = str(mod.get("mcp_server") or "").strip()
    if not server:
        return False
    try:
        from .mcp_client import load_mcp_config, server_map

        spec = server_map(load_mcp_config() or {}).get(server)
    except Exception:  # noqa: BLE001
        return False
    return bool(isinstance(spec, dict) and spec.get("command"))


def is_host_local(source: str, mod: dict[str, Any] | None = None) -> bool:
    src = (source or "").strip().lower()
    if _manifest_host_local(mod):
        return True
    if src in HOST_LOCAL_TOOLS or src.startswith(_HOST_LOCAL_PREFIXES):
        return True
    return _stdio_remote(mod)


def _canonical(source: str, mod: dict[str, Any] | None) -> str:
    if isinstance(mod, dict) and str(mod.get("name") or "").strip():
        return str(mod["name"]).strip().lower()
    return (source or "").strip().lower()


def _bind_session(session: str) -> str:
    """A spawned child turn binds the forwarded parent session."""
    return (
        os.environ.get("AGENTS_PARENT_SESSION", "").strip()
        or (session or "").strip()
    )


def _bind_argv(tmpl: str, session: str, source: str, workspace: object) -> list[str]:
    """Split the template first (Windows keeps backslashes), then format
    placeholders per argv element — substituted values can never inject
    extra arguments."""
    try:
        tokens = shlex.split(tmpl, posix=(os.name != "nt"))
    except ValueError as exc:
        raise SessionGateError(f"bind cmd parse failed: {exc}") from exc
    if os.name == "nt":
        # posix=False keeps quote chars inside the token; strip a matching
        # pair so a quoted path runs as the path, not a string literal.
        tokens = [
            t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t
            for t in tokens
        ]
    ws = str(getattr(workspace, "id", workspace) or "")
    try:
        return [
            token.format(session=session, tool=source, workspace=ws)
            for token in tokens
        ]
    except (KeyError, IndexError, ValueError) as exc:
        raise SessionGateError(f"bind cmd template failed: {exc}") from exc


def _gate_result(out: Any) -> dict[str, Any]:
    """Normalize a bind result to the ``{"bound": bool, ...}`` dict.

    The host gate may answer a bare bool (klanker ``session_bind_gate``):
    ``False`` is a DECLINE and must fail closed — collapsing it into ``{}``
    would run the tool unbound and drop the session worktree. ``True``
    binds with no worktree. Any other non-dict value is unreadable and is
    treated as bound-without-workdir only when it is not a decline."""
    if out is False:
        return {"bound": False}
    if out is True:
        return {"bound": True}
    return out if isinstance(out, dict) else {}


def _run_bind(session: str, source: str, workspace: object) -> dict[str, Any]:
    if _gate_fn is not None:
        if not session:
            raise SessionGateError("session gate hook set but no session id")
        try:
            out = _gate_fn(session, source, workspace)
        except SessionGateError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SessionGateError(f"session gate hook failed: {exc}") from exc
        return _gate_result(out)
    tmpl = os.environ.get("AGENTS_SESSION_BIND_CMD", "").strip()
    if not tmpl:
        return {}
    if not session:
        raise SessionGateError("bind cmd configured but no session id")
    argv = _bind_argv(tmpl, session, source, workspace)
    if not argv:
        raise SessionGateError("bind cmd template produced empty argv")
    try:
        res = subprocess.run(argv, capture_output=True, timeout=10)
    except Exception as exc:  # noqa: BLE001  OSError/TimeoutExpired: fail closed
        raise SessionGateError(f"bind cmd failed: {exc}") from exc
    if res.returncode != 0:
        err = res.stderr.decode(errors="replace")[:200] if res.stderr else ""
        raise SessionGateError(f"bind cmd rc={res.returncode}: {err}")
    try:
        out = json.loads((res.stdout or b"").decode(errors="replace").strip() or "{}")
    except json.JSONDecodeError:
        out = {}
    return _gate_result(out)


def _apply_result(result: dict[str, Any]) -> None:
    """``{"bound": true, "workdir": "..."}`` switches this turn's workdir
    before the triggering tool runs, so the first edit lands in the
    session worktree."""
    if result.get("bound") is False:
        raise SessionGateError("session bind declined by host")
    workdir = str(result.get("workdir") or "").strip()
    if not workdir:
        return
    path = Path(workdir).expanduser()
    if not path.is_dir():
        raise SessionGateError(f"bind returned invalid workdir: {workdir}")
    os.environ["AGENTS_WORKDIR"] = str(path)


def maybe_bind(
    session: str,
    source: str,
    workspace: object = None,
    mod: dict[str, Any] | None = None,
) -> bool:
    """Fire the bind once per process on the first host-local tool.

    Returns True when the gate ran (or nothing is configured), False for
    portable tools and repeats. A configured bind that fails raises
    ``SessionGateError`` without latching ``_fired``.
    """
    global _fired
    if _fired:
        return False
    canonical = _canonical(source, mod)
    if not is_host_local(canonical, mod):
        return False
    result = _run_bind(_bind_session(session), canonical, workspace)
    _apply_result(result)
    _fired = True
    return True


def _reset_for_tests() -> None:
    global _fired
    _fired = False
    clear_session_gate()
