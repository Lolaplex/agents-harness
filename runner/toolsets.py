"""Named toolsets: groups of catalog modules, enabled per channel or flag.

Sets are declared in ``<ws>/config.json`` or ``<host>/agents.json`` under a
``toolsets`` section — each key is a set name, each value a list of module
name globs (``fnmatch``). Members may also be a single string.

Selection order: ``AGENTS_TOOLSET`` (set by ``runner.loop --toolset``) >
config ``toolset`` — a string, or an object ``{"default": "…", "<channel>":
"…"}`` so e.g. Telegram turns get a reduced set. Values ``all``/``off``/
``none``/``0`` or an empty name mean no restriction. An unknown set name is
fail-closed: no modules are exposed.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Any

from .settings import config_value
from .workspace_config import merged_section

_UNRESTRICTED = frozenset({"", "all", "off", "none", "0"})


def defined_sets(workspace: Any = None) -> dict[str, list[str]]:
    """Merged set name → member glob list (host first, workspace wins)."""
    sets: dict[str, list[str]] = {}
    for name, members in merged_section("toolsets", workspace).items():
        if isinstance(members, str):
            members = [members]
        if isinstance(members, list):
            sets[str(name)] = [str(m) for m in members]
    return sets


def active_name(channel: str = "", workspace: Any = None) -> str:
    env = os.environ.get("AGENTS_TOOLSET", "").strip()
    if env:
        return env
    pick = config_value("toolset", workspace)
    if isinstance(pick, dict):
        ch = (channel or "").strip().lower()
        return str(pick.get(ch) or pick.get("default") or "").strip()
    return str(pick or "").strip()


def allowed_patterns(channel: str = "", workspace: Any = None) -> list[str] | None:
    """Member globs of the active set, ``None`` when unrestricted.

    An unknown set name returns ``[]`` — fail closed.
    """
    name = active_name(channel, workspace)
    if name.lower() in _UNRESTRICTED:
        return None
    return list(defined_sets(workspace).get(name) or [])


def module_allowed(name: str, *, channel: str = "", workspace: Any = None) -> bool:
    patterns = allowed_patterns(channel, workspace)
    if patterns is None:
        return True
    needle = (name or "").strip()
    return any(fnmatch.fnmatchcase(needle, p.partition(":")[0]) for p in patterns)


def command_allowed(mod: dict[str, Any], arguments: Any, *, channel: str = "", workspace: Any = None) -> bool:
    """Group filter inside an allowed module. A member ``<glob>:<group>``
    admits only commands whose spec ``groups`` (or implicit ``side_effects``
    class) contains ``<group>`` — ``mcp.browser:read`` exposes just the
    read-only verbs of the browser package. Unresolvable commands fail closed."""
    patterns = allowed_patterns(channel, workspace)
    if patterns is None:
        return True
    name = str(mod.get("name") or "")
    matched: list[str] = []
    for member in patterns:
        base, sep, group = member.partition(":")
        if fnmatch.fnmatchcase(name, base):
            matched.append(group if sep else "")
    if not matched:
        return False
    if "" in matched:
        return True  # full-module membership wins over group-limited entries
    from .helpjson import classify_spec, select_command

    selected = select_command(mod, arguments)
    if selected is None:
        return False
    spec = selected[1]
    groups = spec.get("groups")
    if not isinstance(groups, list):
        groups = [classify_spec(spec, default="mutating") or "mutating"]
    groups = {str(g) for g in groups}
    return any(g in groups for g in matched)


def filter_modules(rows: list[dict[str, Any]], *, channel: str = "", workspace: Any = None) -> list[dict[str, Any]]:
    if allowed_patterns(channel, workspace) is None:
        return rows
    return [m for m in rows if module_allowed(str(m.get("name") or ""), channel=channel, workspace=workspace)]
