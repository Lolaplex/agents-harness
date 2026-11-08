"""Pinned verbs: hot commands of a module in the system prompt.

A module manifest may set ``"pin": true``. Its discovered commands are
rendered as compact ``call_job`` examples so a model does not spend a
``load_schema`` round trip on every file operation. The closed carrier
stays: the three Cordis tools are unchanged.

``AGENTS_PINNED_VERBS=0`` disables the block. Help-json discovery is cached
per process by ``helpjson.load_help_json``; there is no disk cache. The
block is capped at ``MAX_CHARS`` so it cannot blow up the prompt cache.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .helpjson import MUTATING, classify_spec, with_discovered_commands
from .modules import list_modules

MAX_CHARS = 2500
CLIP_MARKER = "[pinned block clipped]"
_FALSY = ("0", "false", "no", "off")
_META_KEYS = ("command", "command_name", "subcommand", "cmd", "name", "argv")

# executor.execute_job runs subprocess.run without stdin. A command that
# needs stdin (edit --stdin-json, write --stdin) cannot be driven from
# call_job, so the examples use the flag form instead.
_STDIN_SUPPORTED = False

_PLACEHOLDERS: dict[str, Any] = {
    "pattern": "TODO",
    "content": "hello world",
    "old": "hello world",
    "new": "hello there",
    "glob": "*",
    "query": "hello",
    "argv": ["python", "-m", "unittest"],
}


def disabled() -> bool:
    return os.environ.get("AGENTS_PINNED_VERBS", "").strip().lower() in _FALSY


def _placeholder(key: str, command: str) -> Any:
    if key == "path":
        return "notes.txt" if command in ("write", "edit") else "README.md"
    return _PLACEHOLDERS.get(key, key)


def _is_positional(usage: str, key: str) -> bool:
    token = key.upper()
    return f"[{token}]" in usage or f"<{token}>" in usage


def _example_arguments(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Named arguments that ``cordis_tools._arguments_to_argv`` maps to argv.

    Required parameters become positionals, optional non-boolean parameters
    without a default become ``--flag value``. Optional positionals (``[PATH]``
    in the usage line) are skipped because the mapper would emit ``--path``,
    which the CLI does not accept.
    """
    raw_params = spec.get("parameters")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    raw_props = params.get("properties")
    props: dict[str, Any] = raw_props if isinstance(raw_props, dict) else {}
    required = [str(item) for item in (params.get("required") or [])]
    usage = str(spec.get("usage") or "")
    args: dict[str, Any] = {"command": name}
    for key in required:
        args[key] = _placeholder(key, name)
    for key, schema in props.items():
        if key in required or key in _META_KEYS:
            continue
        schema = schema if isinstance(schema, dict) else {}
        if schema.get("type") == "boolean":
            continue
        if "default" in schema:
            continue
        if _is_positional(usage, key):
            continue
        args[key] = _placeholder(key, name)
    return args


def _example_call(module_name: str, arguments: dict[str, Any]) -> str:
    return json.dumps(
        {"name": module_name, "arguments": arguments},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def pinned_entries() -> list[dict[str, Any]]:
    """One entry per pinned command, or an ``unavailable`` entry per module.

    A discovery failure is reported as a line instead of crashing or being
    dropped silently.
    """
    entries: list[dict[str, Any]] = []
    for mod in list_modules():
        if not mod.get("pin"):
            continue
        name = str(mod.get("name") or "")
        try:
            discovered = with_discovered_commands(mod)
        except Exception as exc:  # noqa: BLE001 - one bad module must not kill the prompt
            entries.append({"module": name, "unavailable": f"{type(exc).__name__}: {exc}"})
            continue
        commands = discovered.get("commands")
        if not isinstance(commands, dict) or not commands:
            source = str(mod.get("commands_from") or "").strip()
            reason = "help-json unavailable" if source else "no commands declared"
            entries.append({"module": name, "unavailable": reason})
            continue
        for command, spec in commands.items():
            if not isinstance(spec, dict):
                continue
            raw_params = spec.get("parameters")
            params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
            raw_props = params.get("properties")
            props: dict[str, Any] = raw_props if isinstance(raw_props, dict) else {}
            arguments = _example_arguments(str(command), spec)
            entries.append(
                {
                    "module": name,
                    "command": str(command),
                    "side_effects": classify_spec(spec, default=MUTATING) or MUTATING,
                    "arguments": arguments,
                    "call": _example_call(name, arguments),
                    "stdin_option": any(key in props for key in ("stdin", "stdin_json")),
                }
            )
    return entries


def _render(entries: list[dict[str, Any]], limit: int) -> str:
    header = ["Pinned terminal verbs (call_job; no load_schema needed):"]
    command_lines: list[str] = []
    unavailable_lines: list[str] = []
    stdin_option = False
    commands = [entry for entry in entries if "unavailable" not in entry]
    # Mutating verbs first so clipping keeps the calls that need the example most.
    commands.sort(
        key=lambda entry: (
            0 if entry.get("side_effects") == MUTATING else 1,
            str(entry.get("command") or ""),
        )
    )
    for entry in entries:
        if "unavailable" in entry:
            unavailable_lines.append(
                f"pinned: {entry['module']} unavailable ({entry['unavailable']})"
            )
    for entry in commands:
        command_lines.append(
            f"- {entry['command']} [{entry['side_effects']}] {entry['call']}"
        )
        stdin_option = stdin_option or bool(entry.get("stdin_option"))
    notes: list[str] = []
    if stdin_option and not _STDIN_SUPPORTED:
        notes.append(
            "Note: call_job does not pass stdin; use --old/--new (edit) and --content (write)."
        )
    rules = [
        "Rules:",
        "1. Read a file with read before you edit it.",
        "2. Use edit for an existing file; use write only for a new file.",
        "3. Give old enough surrounding context so it matches exactly once.",
        "4. Run tests after changes with run.",
    ]

    def build(commands: list[str], clipped: bool) -> str:
        parts = header + unavailable_lines + commands + notes + rules
        if clipped:
            parts = parts + [CLIP_MARKER]
        return "\n".join(parts)

    text = build(command_lines, False)
    if len(text) <= limit:
        return text
    kept = list(command_lines)
    while kept:
        kept.pop()
        text = build(kept, True)
        if len(text) <= limit:
            return text
    text = build([], True)
    if len(text) <= limit:
        return text
    room = max(0, limit - len(CLIP_MARKER) - 1)
    return text[:room].rstrip() + "\n" + CLIP_MARKER


def pinned_verbs_prompt_block(limit: int = MAX_CHARS) -> str:
    """System-prefix fragment. Empty when disabled or nothing is pinned."""
    if disabled():
        return ""
    try:
        entries = pinned_entries()
    except Exception as exc:  # noqa: BLE001 - a broken catalog must not kill the turn
        return f"pinned: unavailable ({type(exc).__name__}: {exc})"
    if not entries:
        return ""
    return _render(entries, limit)
