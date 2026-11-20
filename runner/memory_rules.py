"""Hard rules from agents-memory as a system-prefix block (Cordis-safe).

Cordis modules stay the tool surface. This only prepends the rendered
``<memory_rules>`` block (same text as ``agents-memory context``) onto the
loop system prefix, next to the untrusted-data fence note.

Soft dependency: import ``agents_memory.rules.render_rules`` when installed,
else ``python -m agents_memory context``. Missing memory, empty vault, or any
failure yields an empty string. Disable with ``AGENTS_MEMORY_RULES=0``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Dict

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")

# Process cache: one fetch per project key ("" = global).
_cache: Dict[str, str] = {}


def disabled() -> bool:
    raw = os.environ.get("AGENTS_MEMORY_RULES", "").strip().lower()
    if raw in _FALSY:
        return True
    if raw in _TRUTHY:
        return False
    return False


def _via_import(project: str) -> str | None:
    try:
        from agents_memory.rules import render_rules  # type: ignore
    except Exception:
        return None
    try:
        return str(render_rules(project or None) or "")
    except Exception:
        return ""


def _via_cli(project: str, workspace=None, extra_env: Dict[str, str] | None = None) -> str:
    cmd = [sys.executable, "-m", "agents_memory", "context"]
    if project:
        cmd.extend(["--project", project])
    if workspace is not None:
        env = workspace.env()
        env["AGENTS_NO_UPDATE_CHECK"] = "1"
    else:
        env = {**os.environ, "AGENTS_NO_UPDATE_CHECK": "1"}
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=2.0,
            check=False,
            env=env,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def render_memory_rules(
    project: str = "", *, refresh: bool = False, workspace=None
) -> str:
    """Return the ``<memory_rules>`` block, or "" when unavailable."""
    if disabled():
        return ""
    key = (project or "").strip()
    if workspace is not None:
        key = f"{getattr(workspace, 'dir', '')}:{key}"
    if not refresh and key in _cache:
        return _cache[key]
    if workspace is not None:
        # Explicit workspace: the import path would resolve the process env.
        block = _via_cli(project or "", workspace)
    else:
        block = _via_import(key)
        if block is None:
            block = _via_cli(key)
    block = (block or "").strip()
    _cache[key] = block
    return block


def _private_persona_block(project: str, workspace=None) -> str:
    """Second ``<memory_rules>`` block from this persona's private store."""
    try:
        from .personas import private_memory_root_from_env

        root = private_memory_root_from_env(workspace)
    except Exception:
        return ""
    if root is None:
        return ""
    extra = {"AGENTS_MEMORY_PATH": str(root), "AGENTS_MEMORY_DIR": str(root)}
    if workspace is not None:
        return (_via_cli(project or "", workspace, extra_env=extra) or "").strip()
    saved = {k: os.environ.get(k) for k in extra}
    try:
        os.environ.update(extra)
        block = _via_import(project or "")
        if block is None:
            block = _via_cli(project or "", extra_env=extra)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return (block or "").strip()


def memory_rules_prompt_block(project: str = "", workspace=None) -> str:
    """System-prefix fragment; empty when there is nothing to inject."""
    block = render_memory_rules(project, workspace=workspace)
    private = _private_persona_block(project, workspace)
    chunks = []
    if block:
        chunks.append(
            "Hard rules from local agent memory (follow these; they are not tool output):\n"
            + block
        )
    if private:
        chunks.append(
            "Hard rules from this persona's private memory (shared only with this persona):\n"
            + private
        )
    return "\n\n".join(chunks)


def clear_cache() -> None:
    _cache.clear()
