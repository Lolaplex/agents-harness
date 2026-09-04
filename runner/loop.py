"""Per-request agent loop: reconstruct the trace, optionally complete, call modules.

NVIDIA-style: no long-lived agent object. Each request rebuilds messages from
traces (who engaged + history + context), POSTs chat/completions, appends the
turn. Tools/skills/A2A are Cordis modules listed here; scheduled jobs stay
under schedules/ and are --call'd when needed. plexd is out of this path.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from .cordis_tools import handle_cordis_tool, is_cordis_tool
from .delivery import bind_delivery
from .executor import execute_job, list_schedules
from .modules import (
    find_module,
    list_modules,
    openai_tool_name,
    openai_tools,
)
from .providers import CompletionRequest, CompletionResult, get_provider, list_providers


def _assemble(session: str, limit: int) -> list[dict[str, Any]]:
    from agents_traces.assemble import assemble_messages

    return assemble_messages(session, limit=limit)


def _record(
    session: str,
    role: str,
    content: str,
    channel: str,
    user: str,
    user_id: str = "",
) -> None:
    from agents_traces.assemble import record_message

    record_message(
        session, role, content, channel=channel, user=user, user_id=user_id
    )


LOOP_TRAILER_MARKER = "---agents-loop-trailer---"


def _emit_trailer(
    emit: Any,
    *,
    session: str,
    user_id: str,
    alias: str,
    mode: str,
) -> None:
    if mode != "buffered":
        return
    trailer = json.dumps(
        {"session": session, "user_id": user_id, "alias": alias},
        ensure_ascii=False,
    )
    print(f"{LOOP_TRAILER_MARKER}\n{trailer}", file=sys.stdout, flush=True)


def _run_tool_calls(calls: list[Any]) -> list[dict[str, Any]]:
    """Route tool_calls through the three Cordis tools (closed carrier)."""
    messages: list[dict[str, Any]] = []
    for i, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else call
        name = str(fn.get("name") or call.get("name") or "").strip()
        call_id = str(call.get("id") or f"call_{i}")
        if is_cordis_tool(name):
            content = handle_cordis_tool(name, call)
        else:
            content = f"refused: unknown tool {name} (use call_job with a catalog name)"
        messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": content}
        )
    return messages


_TOOL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_TOOL_FENCE = re.compile(
    r"```(?:json|tool)?\s*(\{.*?\})\s*```",
    re.DOTALL,
)


def _json_tool_call(blob: str, call_id: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    name = str(obj.get("name") or "").strip()
    args = obj.get("arguments") if "arguments" in obj else obj.get("parameters")
    if not name:
        return None
    if args is None:
        args = {}
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def _parse_text_tool_calls(text: str) -> tuple[list[dict[str, Any]] | None, str]:
    """Local models sometimes emit a tool call as XML/JSON in content."""
    if not text:
        return None, text
    match = _TOOL_TAG.search(text) or _TOOL_FENCE.search(text)
    if not match:
        return None, text
    parsed = _json_tool_call(match.group(1), "text_1")
    if parsed is None:
        return None, text
    rest = (text[: match.start()] + text[match.end() :]).strip()
    return [parsed], rest


def _coerce_tool_calls(result: CompletionResult) -> CompletionResult:
    if result.tool_calls:
        return result
    parsed, rest = _parse_text_tool_calls(result.text or "")
    if not parsed:
        return result
    result.tool_calls = parsed
    result.text = rest
    return result


def _skills_for_prompt() -> list[tuple[str, str]]:
    rows = []
    for m in list_modules():
        summary = str(m.get("rests_on") or m.get("verb") or "")
        rows.append((str(m["name"]), summary))
    return rows


def build_payload(
    session: str,
    user_message: str,
    *,
    limit: int = 24,
    system: str = "",
    include_modules: bool = True,
    user_id: str = "",
    user_display: str = "",
    work: str = "",
    project: str = "",
    timezone_name: str = "",
    aliases: list[str] | None = None,
    start_date: str = "",
    include_clock: bool = True,
) -> list[dict[str, Any]]:
    """system (cached) → history → clock → new user turn.

    Clock is last-before-user so it cannot invalidate the system prefix.
    """
    from agents_traces.prompt import PromptParts, clock_message, system_messages

    parts = PromptParts(
        instructions=system,
        skills=_skills_for_prompt() if include_modules else [],
        user_id=user_id,
        user_display=user_display,
        work=work,
        project=project,
        timezone=timezone_name,
        aliases=list(aliases or []),
        session_id=session,
        start_date=start_date,
    )
    messages: list[dict[str, Any]] = list(system_messages(parts))
    messages.extend(_assemble(session, limit=limit))
    if include_clock:
        messages.append(clock_message(timezone_name=timezone_name))
    if user_message:
        messages.append({"role": "user", "content": user_message})
    return messages


def _resolve_identity(args: argparse.Namespace):
    from agents_traces.identity import IdentityStore, session_has_events

    ident = IdentityStore()
    return ident.resolve(
        channel=args.channel,
        user=args.user,
        user_id=args.user_id,
        session=args.session or "",
        new_session=args.new_session,
        project=args.project,
        legacy_exists=session_has_events,
    )


def _complete_once(provider: Any, req: CompletionRequest) -> CompletionResult:
    return _coerce_tool_calls(provider.complete(req))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-request loop: assemble trace, complete, or call a Cordis module"
    )
    parser.add_argument("--session", help="Thread id (ses_…); omit to resume the user's active session")
    parser.add_argument("--channel", default="local", help="Alias scheme (telegram, http, cli, …)")
    parser.add_argument("--user", default="", help="Channel handle (chat id, login, …)")
    parser.add_argument("--user-id", default="", dest="user_id", help="Canonical person id (binds this alias)")
    parser.add_argument("--new-session", action="store_true", help="Start a new thread for this user")
    parser.add_argument("--project", default="", help="Current project for runtime_context")
    parser.add_argument("--message", default="", help="New user turn")
    parser.add_argument("--limit", type=int, default=24, help="History turns to rebuild")
    parser.add_argument("--system", default="", help="Optional instructions blob (stable prefix)")
    parser.add_argument("--provider", default="", help="Provider name (openai.default, echo, …)")
    parser.add_argument("--assemble-only", action="store_true", help="Print reconstructed payload, no HTTP")
    parser.add_argument("--complete", action="store_true", help="Complete via the selected provider and record")
    parser.add_argument(
        "--deliver",
        default="",
        help="stream (TTY tokens) or buffered (one answer; thinking on stderr). Default from --channel.",
    )
    parser.add_argument("--list-modules", action="store_true", help="List MCP/skill/A2A/schedule modules")
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="List OpenAI tools advertised from as_tool modules",
    )
    parser.add_argument("--list-providers", action="store_true", help="List completion providers")
    parser.add_argument("--call", metavar="NAME", help="Run one module or schedule by name (Cordis)")
    parser.add_argument("--check-term", metavar="FILE", help="Check a job-term JSON against the catalog")
    parser.add_argument("--reduce-term", metavar="FILE", help="Check, mix, and reduce a job-term JSON")
    args = parser.parse_args(argv)

    if args.check_term:
        from .kernel import main as kernel_main

        return kernel_main([args.check_term, "--check"])
    if args.reduce_term:
        from .kernel import main as kernel_main

        return kernel_main([args.reduce_term, "--reduce"])

    if args.list_tools:
        print("tools:")
        for t in openai_tools():
            fn = t["function"]
            print(f"  {fn['name']:<24} {fn.get('description', '')}")
        return 0

    if args.list_modules:
        print("modules:")
        for m in list_modules():
            tool = openai_tool_name(m) if m.get("as_tool") else ""
            extra = f" tool={tool}" if tool else ""
            print(
                f"  {m['name']:<20} kind={m['kind']:<8} when={m['when']:<12} {m['verb']}{extra}"
            )
        print("schedules:")
        for s in list_schedules():
            print(f"  {s['name']:<20} cadence={s['cadence']:<8} {s['verb']}")
        print("providers:")
        for p in list_providers():
            print(f"  {p['name']:<20} kind={p['kind']:<14} {p['verb']}")
        print("tools:")
        for t in openai_tools():
            print(f"  {t['function']['name']}")
        return 0

    if args.list_providers:
        print("providers:")
        for p in list_providers():
            print(f"  {p['name']:<20} kind={p['kind']:<14} {p['verb']}")
        return 0

    if args.call:
        mod = find_module(args.call)
        if mod is None:
            match = [s for s in list_schedules() if s["name"].lower() == args.call.lower()]
            if not match:
                print(f"Error: no module or schedule named '{args.call}'", file=sys.stderr)
                return 1
            res = execute_job(match[0])
        else:
            res = execute_job(mod)
        return 0 if res.get("status") == "SUCCESS" else 1

    if not args.session and not args.user and not args.user_id:
        print("Error: --session, --user, or --user-id is required", file=sys.stderr)
        return 1

    resolved = _resolve_identity(args)
    session = resolved.session.id
    user_text = args.message or ""

    system_text = args.system
    if system_text:
        import os
        from pathlib import Path
        try:
            p = Path(system_text)
            if p.is_file():
                system_text = p.read_text(encoding="utf-8")
        except Exception:
            pass

    messages = build_payload(
        session,
        user_text,
        limit=args.limit,
        system=system_text,
        user_id=resolved.user.id,
        user_display=resolved.user.display,
        work=resolved.user.work,
        project=args.project or resolved.user.project,
        timezone_name=resolved.user.timezone,
        aliases=resolved.user.aliases,
        start_date=resolved.session.start_date,
    )
    if args.assemble_only or not args.complete:
        print(
            json.dumps(
                {
                    "session": session,
                    "user_id": resolved.user.id,
                    "alias": resolved.alias,
                    "start_date": resolved.session.start_date,
                    "messages": messages,
                    "tools": openai_tools(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not args.complete:
            return 0

    if not user_text and not args.assemble_only:
        print("Error: --message is required unless --assemble-only with existing history", file=sys.stderr)
        return 1

    provider = get_provider(args.provider)
    mode, on_status, on_delta, emit = bind_delivery(args.channel, override=args.deliver)
    if user_text:
        _record(session, "user", user_text, args.channel, args.user, resolved.user.id)
    tools = openai_tools()
    req = CompletionRequest(
        messages=messages,
        tools=tools or None,
        on_status=on_status,
        on_delta=on_delta,
    )
    max_tool_rounds = 3
    result = _complete_once(provider, req)
    for _round in range(max_tool_rounds):
        if not result.tool_calls:
            break
        tool_msgs = _run_tool_calls(result.tool_calls)
        messages.append(
            {
                "role": "assistant",
                "content": result.text or None,
                "tool_calls": result.tool_calls,
            }
        )
        messages.extend(tool_msgs)
        result = _complete_once(
            provider,
            CompletionRequest(
                messages=messages,
                tools=tools or None,
                on_status=on_status,
                on_delta=on_delta,
            ),
        )
    else:
        if result.tool_calls:
            print(
                f"Error: still requested tools after {max_tool_rounds} rounds",
                file=sys.stderr,
            )
            return 1
    if result.text:
        _record(session, "assistant", result.text, args.channel, args.user, resolved.user.id)
    emit(result.text)
    _emit_trailer(
        emit,
        session=session,
        user_id=resolved.user.id,
        alias=resolved.alias,
        mode=mode,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
