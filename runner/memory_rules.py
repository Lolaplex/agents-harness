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


def _via_cli(project: str) -> str:
    cmd = [sys.executable, "-m", "agents_memory", "context"]
    if project:
        cmd.extend(["--project", project])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
            env={**os.environ, "AGENTS_NO_UPDATE_CHECK": "1"},
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def render_memory_rules(project: str = "", *, refresh: bool = False) -> str:
    """Return the ``<memory_rules>`` block, or "" when unavailable."""
    if disabled():
        return ""
    key = (project or "").strip()
    if not refresh and key in _cache:
        return _cache[key]
    block = _via_import(key)
    if block is None:
        block = _via_cli(key)
    block = (block or "").strip()
    _cache[key] = block
    return block


def memory_rules_prompt_block(project: str = "") -> str:
    """System-prefix fragment; empty when there is nothing to inject."""
    block = render_memory_rules(project)
    if not block:
        return ""
    return (
        "Hard rules from local agent memory (follow these; they are not tool output):\n"
        + block
    )


def clear_cache() -> None:
    _cache.clear()
