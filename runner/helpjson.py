"""Side-effect class from a help-json document or any tool manifest.

Precedence is ``side_effects``, then ``mutates``, then
``annotations.readOnlyHint`` (a top-level ``readOnlyHint`` or ``read_only``
counts as that same hint). The command name is never evidence.

A command entry with none of those fields is mutating. A module with none of
them is mutating too: ``classify_spec(..., default=None)`` reports that the
metadata is absent, and the approval gate treats that absence as mutating.
The tool name is not a class. Unknown ``side_effects`` values are mutating.

This module does not import agents-terminal or any other tool package.
``commands_from: "help-json"`` optionally runs that module's own verb with
``--help-json`` and caches the document.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

READ_ONLY = "read-only"
MUTATING = "mutating"

_HELP_CACHE: dict[str, dict[str, Any] | None] = {}
_HELP_TIMEOUT_SEC = 3.0
_HELP_STDOUT_MAX = 1_000_000
_META_KEYS = ("command", "command_name", "subcommand", "cmd", "name", "argv", "arguments")


def clear_help_cache() -> None:
    _HELP_CACHE.clear()


def _norm_effects(value: str) -> str:
    token = value.strip().lower().replace("_", "-")
    if token in (READ_ONLY, "readonly"):
        return READ_ONLY
    return MUTATING


def classify_spec(spec: Any, *, default: str | None = MUTATING) -> str | None:
    """Return ``read-only``, ``mutating``, or ``default`` when no metadata is set.

    ``default=None`` lets a caller keep a legacy fallback. A command entry
    passes the mutating default so a missing class is not guessed from the name.
    """
    if not isinstance(spec, dict):
        return default
    raw_effects = spec.get("side_effects")
    if "side_effects" in spec and str(raw_effects or "").strip():
        return _norm_effects(str(raw_effects))
    if "mutates" in spec:
        return MUTATING if bool(spec.get("mutates")) else READ_ONLY
    ann = spec.get("annotations")
    if isinstance(ann, dict) and "readOnlyHint" in ann:
        return READ_ONLY if bool(ann.get("readOnlyHint")) else MUTATING
    if "readOnlyHint" in spec:
        return READ_ONLY if bool(spec.get("readOnlyHint")) else MUTATING
    if "read_only" in spec:
        return READ_ONLY if bool(spec.get("read_only")) else MUTATING
    return default


def command_index(manifest: Any) -> dict[str, tuple[str, dict[str, Any]]]:
    """Map each command name and alias to ``(canonical, spec)``."""
    if not isinstance(manifest, dict):
        return {}
    commands = manifest.get("commands")
    if not isinstance(commands, dict):
        return {}
    index: dict[str, tuple[str, dict[str, Any]]] = {}
    for name, spec in commands.items():
        if not isinstance(spec, dict):
            continue
        canonical = str(name)
        index[canonical] = (canonical, spec)
        aliases = spec.get("aliases")
        if isinstance(aliases, list):
            for alias in aliases:
                token = str(alias).strip()
                if token:
                    index[token] = (canonical, spec)
    return index


def unwrap_arguments(arguments: Any) -> Any:
    if isinstance(arguments, dict) and set(arguments.keys()) == {"arguments"}:
        return unwrap_arguments(arguments.get("arguments"))
    return arguments


def select_command(manifest: Any, arguments: Any) -> tuple[str, dict[str, Any]] | None:
    """Resolve a declared command from arguments. Names only look up metadata.

    ``command`` matches only when the whole string is a command or alias.
    A shell line is not split, so ``git status`` does not become a command
    named ``git``. ``argv[0]`` matches when that token is declared.
    """
    index = command_index(manifest)
    if not index:
        nested = manifest.get("help_json") if isinstance(manifest, dict) else None
        if isinstance(nested, dict) and nested is not manifest:
            return select_command(nested, arguments)
        return None
    args = unwrap_arguments(arguments)
    if isinstance(args, str):
        token = args.strip()
        return index.get(token)
    if isinstance(args, list):
        if args and str(args[0]) in index:
            return index[str(args[0])]
        return None
    if not isinstance(args, dict):
        return None
    for key in ("command_name", "subcommand"):
        val = args.get(key)
        if isinstance(val, str) and val.strip() in index:
            return index[val.strip()]
    argv = args.get("argv")
    if isinstance(argv, list) and argv and str(argv[0]) in index:
        return index[str(argv[0])]
    command = args.get("command")
    if isinstance(command, str) and command.strip() in index:
        return index[command.strip()]
    cmd = args.get("cmd")
    if isinstance(cmd, str) and cmd.strip() in index and "command" not in args:
        return index[cmd.strip()]
    name = args.get("name")
    if (
        isinstance(name, str)
        and name.strip() in index
        and "command" not in args
        and "argv" not in args
        and "command_name" not in args
    ):
        return index[name.strip()]
    return None


def call_effects(manifest: Any, arguments: Any) -> str | None:
    """Class of the invoked command, or None when arguments name no command.

    A selected command with no side-effect fields is mutating.
    """
    selected = select_command(manifest, arguments)
    if selected is None:
        return None
    return classify_spec(selected[1], default=MUTATING)


def _truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val == 1
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _wants_double_dash(spec: dict[str, Any]) -> bool:
    usage = str(spec.get("usage") or "")
    return " -- " in usage or usage.rstrip().endswith(" --")


def _schema_args(spec: dict[str, Any], arguments: dict[str, Any], invoked: str) -> list[str]:
    params = spec.get("parameters") if isinstance(spec.get("parameters"), dict) else {}
    props = params.get("properties") if isinstance(params, dict) and isinstance(params.get("properties"), dict) else {}
    required = [str(item) for item in (params.get("required") or [])] if isinstance(params, dict) else []
    out: list[str] = []
    for key, schema in props.items():
        if key in _META_KEYS:
            continue
        if key not in arguments or arguments[key] in (None, ""):
            continue
        schema = schema if isinstance(schema, dict) else {}
        flag = "--" + key.replace("_", "-")
        if schema.get("type") == "boolean":
            if _truthy(arguments[key]):
                out.append(flag)
            continue
        if schema.get("type") == "array" and isinstance(arguments[key], list):
            body = [str(item) for item in arguments[key]]
            if key == "argv" and _wants_double_dash(spec):
                if body[:1] == ["--"]:
                    body = body[1:]
                return [invoked, "--", *body]
            if key in required:
                out.extend(body)
            else:
                out.append(flag)
                out.extend(body)
            continue
        if key in required:
            out.append(str(arguments[key]))
            continue
        out.extend([flag, str(arguments[key])])
    return [invoked, *out]


def declared_argv(mod: Any, arguments: Any) -> list[str] | None:
    """Argv for a declared command, or None so the caller keeps its own builder.

    The invoked token is preserved, including aliases. A ``run``-style usage
    that contains `` -- `` inserts that separator in front of an argv array.
    """
    selected = select_command(mod, arguments)
    if selected is None:
        return None
    canonical, spec = selected
    args = unwrap_arguments(arguments)
    names = {canonical}
    aliases = spec.get("aliases") if isinstance(spec.get("aliases"), list) else []
    names.update(str(alias).strip() for alias in aliases if str(alias).strip())
    if isinstance(args, list) and args and str(args[0]) in names:
        head = str(args[0])
        rest = [str(item) for item in args[1:]]
        if _wants_double_dash(spec) and rest[:1] != ["--"]:
            return [head, "--", *rest]
        return [head, *rest]
    if not isinstance(args, dict):
        return [canonical]
    argv = args.get("argv")
    if isinstance(argv, list) and argv:
        head = str(argv[0])
        if head in names:
            rest = [str(item) for item in argv[1:]]
            if _wants_double_dash(spec) and rest[:1] != ["--"]:
                return [head, "--", *rest]
            return [head, *rest]
        if _wants_double_dash(spec):
            body = [str(item) for item in argv]
            if body[:1] == ["--"]:
                body = body[1:]
            return [canonical, "--", *body]
    invoked = canonical
    for key in ("command_name", "subcommand", "command", "cmd", "name"):
        val = args.get(key)
        if isinstance(val, str) and val.strip() in names:
            invoked = val.strip()
            break
    return _schema_args(spec, args, invoked)


def load_help_json(verb: str) -> dict[str, Any] | None:
    """Run ``<verb> --help-json`` once and cache the object. Failures cache None."""
    key = str(verb or "").strip()
    if not key:
        return None
    if key in _HELP_CACHE:
        return _HELP_CACHE[key]
    from .executor import verb_to_argv

    argv = verb_to_argv(key)
    if "--help-json" not in argv:
        argv.append("--help-json")
    doc: dict[str, Any] | None = None
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=_HELP_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout:
        raw = proc.stdout[:_HELP_STDOUT_MAX]
        start = raw.find("{")
        if start >= 0:
            try:
                parsed = json.loads(raw[start:])
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("commands"), dict):
                doc = parsed
    _HELP_CACHE[key] = doc
    return doc


def with_discovered_commands(mod: dict[str, Any]) -> dict[str, Any]:
    """Copy ``mod`` with ``commands`` filled in when the manifest asks for help-json.

    Embedded ``commands`` win and no process is started. Any fetch failure
    leaves the module unchanged (callers keep ``mutates`` and the other flags).
    """
    if isinstance(mod.get("commands"), dict) and mod.get("commands"):
        return mod
    source = str(mod.get("commands_from") or "").strip().lower().replace("_", "-")
    if source not in {"help-json"}:
        return mod
    doc = load_help_json(str(mod.get("verb") or ""))
    if not isinstance(doc, dict):
        return mod
    merged = dict(mod)
    merged["commands"] = doc.get("commands")
    if isinstance(doc.get("classification"), dict):
        merged["classification"] = doc["classification"]
    return merged
