"""Three always-on Cordis tools: list_catalog, load_schema, call_job."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .approval import call_is_mutator, gate_module
from .fence import HarnessError
from .executor import execute_job
from .helpjson import declared_argv, with_discovered_commands
from .modules import find_module, find_module_for_tool, list_modules, openai_tool_name
from .adapter_evidence import _snapshot_arguments, open_invocation
from .receipts import harness_failure, make_receipt, subprocess_succeeded
from .redact import redact_tool_output
from .retrieve import DEFAULT_READ_LIMIT, annotate_clip, read_content, render_read_result
from .skills import format_skill_list, load_skill

_SKILL_MODULES = ("skill.list", "skill.load", "skill.catalog")

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
            return {"arguments": raw.strip()}
    elif isinstance(raw, (dict, list)):
        args = raw
    else:
        args = {}
    return args if isinstance(args, dict) else {"arguments": args}


def _cli_flag_set(val: Any) -> bool:
    """True for a boolean CLI switch. False, missing, and the string false stay off."""
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val == 1
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _arguments_to_argv(mod: dict[str, Any], arguments: Any) -> list[str]:
    if not arguments:
        return []
    if isinstance(arguments, str):
        return [arguments.strip()] if arguments.strip() else []
    if isinstance(arguments, list):
        return [str(a) for a in arguments if str(a).strip()]
    if not isinstance(arguments, dict):
        return []

    # Unwrap single wrapper key: {"arguments": ...}
    if len(arguments) == 1 and "arguments" in arguments:
        inner = arguments["arguments"]
        if isinstance(inner, dict):
            arguments = inner
        elif isinstance(inner, list):
            return [str(a) for a in inner if str(a).strip()]
        elif isinstance(inner, str):
            return [inner.strip()] if inner.strip() else []

    built = declared_argv(mod, arguments)
    if built is not None:
        return built

    name = str(mod.get("name") or "")
    if name == "mcp.terminal":
        raw_cmd = arguments.get("argv") or arguments.get("command") or arguments.get("cmd") or []
        if isinstance(raw_cmd, str):
            cmd_str = raw_cmd.strip()
            # If command uses shell features ($VAR, |, &&, ;, >, <, `), wrap in shell
            has_shell_chars = any(ch in cmd_str for ch in ("$", "|", "&&", ";", ">", "<", "`"))
            if has_shell_chars:
                import sys
                if sys.platform == "win32":
                    return ["run", "--", "powershell", "-NoProfile", "-Command", cmd_str]
                return ["run", "--", "sh", "-c", cmd_str]
            import shlex
            cmd_list = shlex.split(cmd_str)
        elif isinstance(raw_cmd, list):
            cmd_list = [str(a) for a in raw_cmd]
        else:
            cmd_list = []
        # Strip leading 'run' or '--' if LLM repeated them
        while cmd_list and cmd_list[0] in ("run", "--"):
            cmd_list.pop(0)
        return ["run", "--"] + cmd_list if cmd_list else []
    if name == "mcp.memory.add":
        if "argv" in arguments and isinstance(arguments["argv"], list):
            arg_list = [str(a) for a in arguments["argv"]]
            while arg_list and arg_list[0] == "add":
                arg_list.pop(0)
            return arg_list
        fact = arguments.get("fact")
        if fact in (None, "") and arguments.get("text") not in (None, ""):
            fact = arguments.get("text")
        if fact not in (None, ""):
            out = [str(fact)]
            for k in ("kind", "name", "project", "collection"):
                val = arguments.get(k)
                if val not in (None, ""):
                    out.extend([f"--{k}", str(val)])
            return out
    if name == "mcp.memory.read":
        file_val = (
            arguments.get("file_id")
            or arguments.get("file")
            or arguments.get("path")
            or arguments.get("filename")
            or arguments.get("target")
        )
        if file_val not in (None, ""):
            return [str(file_val).strip()]
    if "argv" in arguments and isinstance(arguments["argv"], list):
        return [str(a) for a in arguments["argv"]]

    if name == "mcp.schedule.add":
        out = []
        for k in (
            "at",
            "text",
            "name",
            "cron",
            "cadence",
            "channel",
            "user",
            "verb",
            "timezone",
            "prompt",
            "session",
        ):
            val = arguments.get(k)
            if val not in (None, ""):
                out.extend([f"--{k}", str(val)])
        timeout = arguments.get("timeout_sec")
        if timeout in (None, ""):
            timeout = arguments.get("timeout")
        if timeout not in (None, ""):
            out.extend(["--timeout", str(timeout)])
        grace = arguments.get("grace_min")
        if grace in (None, ""):
            grace = arguments.get("grace")
        if grace not in (None, ""):
            out.extend(["--grace", str(grace)])
        if arguments.get("one_shot"):
            out.append("--one-shot")
        return out
    if name == "mcp.schedule.remove":
        target = arguments.get("name") or arguments.get("slug") or arguments.get("id") or ""
        return [str(target)] if target else []
    if name == "mcp.schedule.list":
        return []
    if name == "mcp.calendar.add":
        out = []
        summary = arguments.get("summary")
        dtstart = arguments.get("dtstart")
        dtend = arguments.get("dtend")
        if summary:
            out.extend(["--summary", str(summary)])
        if dtstart:
            out.extend(["--dtstart", str(dtstart)])
        if dtend:
            out.extend(["--dtend", str(dtend)])
        for k in ("calendar", "location", "description", "timezone"):
            val = arguments.get(k)
            if val not in (None, ""):
                out.extend([f"--{k}", str(val)])
        if _cli_flag_set(arguments.get("all_day")):
            out.append("--all-day")
        return out
    if name == "mcp.calendar.list":
        out = []
        start = arguments.get("from") or arguments.get("start")
        end = arguments.get("to") or arguments.get("end")
        if start:
            out.extend(["--from", str(start)])
        if end:
            out.extend(["--to", str(end)])
        for k in ("calendar", "timezone"):
            val = arguments.get(k)
            if val not in (None, ""):
                out.extend([f"--{k}", str(val)])
        return out
    if name == "mcp.calendar.calendars":
        return []
    if name == "mcp.calendar.update":
        out = []
        for flag, key in (
            ("--href", "href"),
            ("--etag", "etag"),
            ("--summary", "summary"),
            ("--dtstart", "dtstart"),
            ("--dtend", "dtend"),
            ("--location", "location"),
            ("--description", "description"),
            ("--timezone", "timezone"),
        ):
            val = arguments.get(key)
            if val not in (None, ""):
                out.extend([flag, str(val)])
        return out
    if name == "mcp.calendar.delete":
        out = []
        for flag, key in (("--href", "href"), ("--etag", "etag")):
            val = arguments.get(key)
            if val not in (None, ""):
                out.extend([flag, str(val)])
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
        # If no explicit prop matched, but arguments has a single string/list value
        if len(arguments) == 1:
            only_val = next(iter(arguments.values()))
            if isinstance(only_val, str) and only_val.strip():
                return [only_val.strip()]
            if isinstance(only_val, list):
                return [str(a) for a in only_val if str(a).strip()]
    return []


def _skill_arguments_name(job_args: Any) -> str:
    if isinstance(job_args, dict):
        if len(job_args) == 1 and "arguments" in job_args:
            return _skill_arguments_name(job_args["arguments"])
        return str(job_args.get("name") or job_args.get("skill") or "").strip()
    if isinstance(job_args, str):
        return job_args.strip()
    return ""


def _run_skill(mod_name: str, job_args: Any) -> str | None:
    if mod_name in ("skill.list", "skill.catalog"):
        return format_skill_list()
    return load_skill(_skill_arguments_name(job_args))


def _remote_arguments(job_args: Any) -> dict[str, Any]:
    if not isinstance(job_args, dict):
        return {}
    if set(job_args.keys()) == {"arguments"} and isinstance(job_args.get("arguments"), dict):
        return job_args["arguments"]
    inner = job_args.get("arguments")
    if isinstance(inner, dict) and "name" not in job_args:
        return inner
    return {k: v for k, v in job_args.items() if k != "name"}


def _visible_output(text: str, *, session_id: str = "") -> str:
    """Redact, then cap. Clipped bodies stay retrievable by digest."""
    return annotate_clip(redact_tool_output(text or ""), session_id=session_id)


def _job_visible_text(rec: dict[str, Any], *, failed: bool, session_id: str = "") -> tuple[str, str]:
    """Prefer the full stream. Fall back to the trace tail for older records.

    The first string is what the model sees. The second is the full redacted
    stream a receipt payload is taken from, before the cap.
    """
    if failed:
        full = rec.get("stderr")
        if not full:
            full = rec.get("stderr_tail") or rec.get("status") or "tool failed"
        redacted = redact_tool_output(str(full))
        return _visible_output(str(full), session_id=session_id), redacted
    full = rec.get("stdout")
    if full is None:
        full = rec.get("stdout_tail") or ""
    redacted = redact_tool_output(str(full))
    return _visible_output(str(full), session_id=session_id), redacted


@dataclass
class CordisOutcome:
    """In-process result. ``text`` is what ``handle_cordis_tool`` already returned."""

    text: str
    success: bool
    tool: str
    mutating: bool
    arguments: Any
    receipt_source: str = ""
    invocation_id: str = ""
    call_outcome: str = ""


def _outcome(
    text: str,
    *,
    success: bool,
    tool: str,
    mutating: bool = False,
    arguments: Any = None,
    receipt_source: str = "",
    call_outcome: str | None = None,
    raw: Any = None,
) -> CordisOutcome:
    kept = arguments if arguments is not None else {}
    invocation_id = ""
    recorded = ""
    if call_outcome is not None:
        record = open_invocation(tool, kept, call_outcome, raw)
        invocation_id = record.invocation_id
        recorded = record.outcome
    return CordisOutcome(
        text=text,
        success=success,
        tool=tool,
        mutating=mutating,
        arguments=kept,
        receipt_source=receipt_source if receipt_source else text,
        invocation_id=invocation_id,
        call_outcome=recorded,
    )


def _read_int(value: Any, default: int) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _run_read_content(arguments: Any, *, session: str) -> CordisOutcome:
    args = arguments if isinstance(arguments, dict) else {}
    if set(args.keys()) == {"arguments"} and isinstance(args.get("arguments"), dict):
        args = args["arguments"]
    digest = str(args.get("digest") or args.get("sha256") or "").strip()
    if not digest:
        return _outcome(HarnessError("error: read_content requires digest"), success=False, tool="read_content")
    offset = _read_int(args.get("offset"), 0)
    if offset is None:
        return _outcome(HarnessError("error: read_content offset must be an integer"), success=False, tool="read_content")
    if "limit" in args and args.get("limit") not in (None, ""):
        limit = _read_int(args.get("limit"), 0)
        if limit is None:
            return _outcome(HarnessError("error: read_content limit must be an integer"), success=False, tool="read_content")
    else:
        limit = DEFAULT_READ_LIMIT
    session_id = str(args.get("session_id") or args.get("session") or session or "")
    page = read_content(session_id, digest, offset=offset, limit=limit)
    text = render_read_result(page)
    return _outcome(text, success=bool(page.get("available")), tool="read_content", arguments=args)


def run_cordis_tool(
    name: str,
    call: dict[str, Any],
    *,
    default_user: str = "",
    default_channel: str = "",
    default_timezone: str = "",
    session: str = "",
) -> CordisOutcome:
    """Execute one Cordis tool. Receipts are derived from this outcome."""
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
        text = _visible_output("\n".join(lines) if lines else "(empty catalog)", session_id=session)
        return _outcome(text, success=True, tool="list_catalog", arguments=args)
    if tool == "load_schema":
        mod_name = str(args.get("name") or "").strip()
        mod = find_module(mod_name)
        if mod is None:
            return _outcome(f"unknown module {mod_name}", success=False, tool="load_schema")
        schema = mod.get("parameters") or {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query or CLI argument"}
            },
        }
        body = json.dumps(
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
        return _outcome(
            _visible_output(body, session_id=session),
            success=True,
            tool="load_schema",
            arguments=args,
        )
    if tool == "read_content":
        return _run_read_content(args, session=session)
    if tool == "call_job":
        mod_name = str(args.get("name") or "").strip()
        if is_cordis_tool(mod_name) and mod_name != "call_job":
            inner_call = {"function": {"arguments": args.get("arguments") or args}}
            inner = run_cordis_tool(
                mod_name,
                inner_call,
                default_user=default_user,
                default_channel=default_channel,
                default_timezone=default_timezone,
                session=session,
            )
            return inner
        if mod_name == "read_content":
            return _run_read_content(args.get("arguments") if args.get("arguments") is not None else args, session=session)
        mod = find_module(mod_name)
        if mod is None:
            mod = find_module_for_tool(mod_name)
        if mod is None:
            return _outcome(
                HarnessError(f"refused: unknown module {mod_name}"),
                success=False,
                tool=mod_name or "call_job",
                call_outcome="unknown",
            )
        mod = with_discovered_commands(mod)
        job_args = args.get("arguments")
        if job_args is None:
            job_args = {k: v for k, v in args.items() if k != "name"}
        if str(mod.get("name") or "") == "mcp.schedule.add" and isinstance(job_args, dict):
            job_args = dict(job_args)
            if not job_args.get("user") and default_user:
                job_args["user"] = default_user
            if not job_args.get("channel") and default_channel:
                job_args["channel"] = default_channel
            if not job_args.get("timezone") and default_timezone:
                job_args["timezone"] = default_timezone
        mutating = call_is_mutator(mod, job_args)
        allowed, denial = gate_module(
            mod,
            job_args if isinstance(job_args, dict) else {"arguments": job_args},
            session=session,
            user=default_user,
        )
        tool_name = str(mod.get("name") or mod_name)
        # Before the tool runs. The callee may mutate job_args; the record keeps this copy.
        sealed = _snapshot_arguments(job_args)
        if not allowed:
            return _outcome(
                HarnessError(redact_tool_output(denial)),
                success=False,
                tool=tool_name,
                mutating=mutating,
                arguments=sealed,
                call_outcome="denied",
            )
        if tool_name in _SKILL_MODULES:
            loaded = _run_skill(tool_name, job_args)
            if loaded is None:
                missing = HarnessError(f"unknown skill {_skill_arguments_name(job_args)}")
                return _outcome(
                    missing,
                    success=False,
                    tool=tool_name,
                    mutating=mutating,
                    arguments=job_args,
                    receipt_source=missing,
                )
            text = _visible_output(loaded, session_id=session)
            return _outcome(
                text,
                success=not harness_failure(text),
                tool=tool_name,
                mutating=mutating,
                arguments=job_args,
                receipt_source=text,
            )
        if tool_name == "read_content":
            return _run_read_content(job_args, session=session)
        if str(mod.get("kind") or "") == "mcp_remote":
            from .mcp_client import RemoteToolError, call_remote

            try:
                remote_out = call_remote(
                    str(mod.get("mcp_server") or ""),
                    str(mod.get("mcp_tool") or ""),
                    _remote_arguments(job_args),
                )
            except Exception as exc:
                message = HarnessError(f"error: mcp remote failed: {exc}")
                return _outcome(
                    message,
                    success=False,
                    tool=tool_name,
                    mutating=mutating,
                    arguments=sealed,
                    receipt_source=message,
                    call_outcome="failed",
                )
            failed = isinstance(remote_out, RemoteToolError)
            redacted = redact_tool_output(remote_out or "")
            return _outcome(
                _visible_output(remote_out or "", session_id=session),
                success=not failed,
                tool=tool_name,
                mutating=mutating,
                arguments=sealed,
                receipt_source=redacted,
                call_outcome="failed" if failed else "succeeded",
                raw=remote_out,
            )
        extra = _arguments_to_argv(mod, job_args)
        rec = execute_job(mod, extra_argv=extra or None)
        ok = subprocess_succeeded(rec)
        raw_status = str(rec.get("status") or "").strip()
        status = raw_status.upper()
        if ok:
            shown, source = _job_visible_text(rec, failed=False, session_id=session)
        else:
            err = str(rec.get("stderr") or rec.get("stderr_tail") or "").strip()
            if err:
                shown, source = _job_visible_text(rec, failed=True, session_id=session)
            else:
                if status == "TIMEOUT":
                    label = "error: timeout"
                elif not status or status == "SUCCESS":
                    label = "error: non-zero exit"
                elif raw_status.lower().startswith("error:"):
                    label = raw_status
                else:
                    label = f"error: {status}"
                shown = HarnessError(label)
                source = shown
        if status == "TIMEOUT":
            call_outcome = "timed_out"
        elif ok and not harness_failure(shown):
            call_outcome = "succeeded"
        else:
            call_outcome = "failed"
        return _outcome(
            shown,
            success=ok and not harness_failure(shown),
            tool=tool_name,
            mutating=mutating,
            arguments=sealed,
            receipt_source=source,
            call_outcome=call_outcome,
            raw=rec,
        )
    return _outcome(HarnessError(f"unknown cordis tool {name}"), success=False, tool=name)


def receipt_for(outcome: CordisOutcome) -> dict[str, Any] | None:
    success = bool(outcome.success) and not harness_failure(outcome.text) and not harness_failure(outcome.receipt_source)
    return make_receipt(
        outcome.tool,
        outcome.arguments,
        outcome.receipt_source,
        success=success,
        mutating=outcome.mutating,
    )


def handle_cordis_tool(
    name: str,
    call: dict[str, Any],
    *,
    default_user: str = "",
    default_channel: str = "",
    default_timezone: str = "",
    session: str = "",
) -> str:
    return run_cordis_tool(
        name,
        call,
        default_user=default_user,
        default_channel=default_channel,
        default_timezone=default_timezone,
        session=session,
    ).text


def is_cordis_tool(name: str) -> bool:
    return name.strip().lower() in CORDIS_TOOL_NAMES
