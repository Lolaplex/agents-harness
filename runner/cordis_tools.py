"""Three always-on Cordis tools: list_catalog, load_schema, call_job."""

from __future__ import annotations

import json
from typing import Any

from .executor import execute_job
from .modules import find_module, list_modules, openai_tool_name
from .redact import redact_tool_output

CORDIS_TOOL_NAMES = ("list_catalog", "load_schema", "call_job")

_LIST_CATALOG_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "description": "Optional filter: mcp, skill, a2a, schedule",
        }
    },
}

_LOAD_SCHEMA_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Cordis module name (e.g. mcp.memory.search)",
        }
    },
    "required": ["name"],
}

_CALL_JOB_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Cordis module name from the catalog",
        },
        "arguments": {
            "type": "object",
            "description": "Module-specific arguments (e.g. query for search)",
        },
    },
    "required": ["name"],
}


def openai_cordis_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_catalog",
                "description": "List Cordis job modules available on this machine",
                "parameters": _LIST_CATALOG_SCHEMA,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "load_schema",
                "description": "Load parameter schema for one catalog module by name",
                "parameters": _LOAD_SCHEMA_SCHEMA,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "call_job",
                "description": "Run one catalog module by name. Unknown names fail closed.",
                "parameters": _CALL_JOB_SCHEMA,
            },
        },
    ]


def _parse_arguments(call: dict[str, Any]) -> dict[str, Any]:
    fn = call.get("function") if isinstance(call.get("function"), dict) else call
    raw = fn.get("arguments") or "{}"
    if isinstance(raw, str):
        try:
            args = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {}
    elif isinstance(raw, dict):
        args = raw
    else:
        args = {}
    return args if isinstance(args, dict) else {}


def _arguments_to_argv(mod: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    if not arguments:
        return []
    if "argv" in arguments and isinstance(arguments["argv"], list):
        argv = [str(a) for a in arguments["argv"]]
        name = str(mod.get("name") or "")
        if name == "mcp.terminal" and argv and argv[0] != "run":
            return ["run", "--"] + argv
        return argv
    name = str(mod.get("name") or "")
    if name == "mcp.memory.add" and "fact" in arguments:
        out = [str(arguments["fact"])]
        for k in ("kind", "name", "project", "collection"):
            val = arguments.get(k)
            if val not in (None, ""):
                out.extend([f"--{k}", str(val)])
        return out
    query = str(arguments.get("query") or arguments.get("q") or "").strip()
    if query:
        return [query]
    params = mod.get("parameters") or {}
    props = params.get("properties") if isinstance(params, dict) else None
    if isinstance(props, dict):
        out: list[str] = []
        for key in props:
            if key in arguments and arguments[key] not in (None, ""):
                out.append(str(arguments[key]))
        if out:
            return out
    return []


def handle_cordis_tool(name: str, call: dict[str, Any]) -> str:
    tool = name.strip().lower()
    args = _parse_arguments(call)
    if tool == "list_catalog":
        kind = str(args.get("kind") or "").strip().lower()
        rows = list_modules()
        if kind:
            rows = [m for m in rows if str(m.get("kind") or "").lower() == kind]
        lines = [
            f"{m['name']}\tkind={m.get('kind')}\twhen={m.get('when')}\t{m.get('rests_on', '')}"
            for m in rows
        ]
        return "\n".join(lines) if lines else "(empty catalog)"
    if tool == "load_schema":
        mod_name = str(args.get("name") or "").strip()
        mod = find_module(mod_name)
        if mod is None:
            return f"unknown module {mod_name}"
        schema = mod.get("parameters") or {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query or CLI argument"}
            },
        }
        return json.dumps(
            {
                "name": mod["name"],
                "kind": mod.get("kind"),
                "verb": mod.get("verb"),
                "tool_name": openai_tool_name(mod),
                "parameters": schema,
            },
            ensure_ascii=False,
            indent=2,
        )
    if tool == "call_job":
        mod_name = str(args.get("name") or "").strip()
        mod = find_module(mod_name)
        if mod is None:
            return f"refused: unknown module {mod_name}"
        job_args = args.get("arguments")
        if not isinstance(job_args, dict):
            job_args = {k: v for k, v in args.items() if k != "name"}
        extra = _arguments_to_argv(mod, job_args)
        rec = execute_job(mod, extra_argv=extra or None)
        if rec.get("status") != "SUCCESS":
            return redact_tool_output(rec.get("stderr_tail") or rec.get("status") or "tool failed")
        return redact_tool_output(rec.get("stdout_tail") or "")
    return f"unknown cordis tool {name}"


def is_cordis_tool(name: str) -> bool:
    return name.strip().lower() in CORDIS_TOOL_NAMES
