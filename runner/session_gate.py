"""Session bind gate (klanker#145 R6 / harness#38 contract).

The first HOST-LOCAL tool call in a loop process marks the session as
executing on this host. The harness owns no bind semantics — it fires
``AGENTS_SESSION_BIND_CMD`` once per process, formatted with
``{session}``, ``{tool}`` and ``{workspace}`` placeholders. The host app
(klanker) maps that to a ``session:host`` datom; without the env var
the gate is a no-op.

Host-local = tools that touch this host's filesystem, processes or
devices (terminal/git/fs/proc/browser/delegate/pre_pr/vision). ws-data
tools (memory/calendar/traces/persona/schedule/skills) stay portable
and never bind.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess

log = logging.getLogger(__name__)

HOST_LOCAL_TOOLS = frozenset({
    "git", "mcp.terminal", "mcp.browser", "mcp.delegate",
    "repo.pre_pr", "vision.analyze", "read_content",
    "fs", "proc", "process", "terminal", "workdir",
})

_fired = False
_gate_fn = None


def register_session_gate(fn):
    """In-process bind hook (called once per process, first host-local
    tool). Preferred over the env cmd when the host registers one —
    e.g. klanker.turn writes ``session:host`` directly into its store."""
    global _gate_fn
    _gate_fn = fn
    return fn


def clear_session_gate() -> None:
    global _gate_fn
    _gate_fn = None


def is_host_local(source: str) -> bool:
    src = (source or "").strip()
    if src in HOST_LOCAL_TOOLS:
        return True
    return any(
        src.startswith(p) for p in (
            "mcp.terminal.", "mcp.fs", "mcp.proc", "mcp.process",
            "fs.", "proc.", "git.", "terminal."))


def maybe_bind(session: str, source: str, workspace: object = None) -> bool:
    """Fire the bind command once per process on the first host-local
    tool. Returns True when the command ran (regardless of its rc)."""
    global _fired
    if _fired or not is_host_local(source):
        return False
    _fired = True
    if _gate_fn is not None:
        try:
            _gate_fn(session, source, workspace)
        except Exception as exc:  # noqa: BLE001
            log.warning("session gate hook failed: %s", exc)
        return _fired
    tmpl = os.environ.get("AGENTS_SESSION_BIND_CMD", "").strip()
    if not tmpl or not session:
        return _fired
    try:
        cmd = tmpl.format(
            session=session,
            tool=source,
            workspace=str(getattr(workspace, "id", workspace) or ""))
    except Exception as exc:  # noqa: BLE001
        log.warning("session bind cmd format failed: %s", exc)
        return _fired
    try:
        res = subprocess.run(
            shlex.split(cmd), capture_output=True, timeout=10)
        if res.returncode != 0:
            log.warning("session bind cmd rc=%s: %s",
                        res.returncode, res.stderr.decode()[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("session bind cmd failed: %s", exc)
    return _fired


def _reset_for_tests() -> None:
    global _fired
    _fired = False
    clear_session_gate()
