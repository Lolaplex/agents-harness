"""Per-request agent loop: reconstruct the trace, optionally complete, call modules.

NVIDIA-style: no long-lived agent object. Each request rebuilds messages from
traces (who engaged + history + context), POSTs chat/completions, appends the
turn. Tools/skills/A2A are Cordis modules listed here; scheduled jobs stay
under schedules/ and are --call'd when needed. plexd is out of this path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from typing import Any, Callable

from .attachments import render_user_content, vision_enabled
from .cordis_tools import handle_cordis_tool, is_cordis_tool
from .fence import FENCE_SYSTEM_NOTE, HarnessMessage, fence_untrusted, present_tool_result
from .delivery import bind_delivery
from .executor import execute_job, list_schedules
from .modules import (
    find_module,
    list_modules,
    openai_tool_name,
    openai_tools,
)
from .providers import CompletionRequest, CompletionResult, find_provider, get_provider, list_providers
from .skills import skills_prompt_block
from .memory_rules import memory_rules_prompt_block


def _assemble(session: str, limit: int) -> list[dict[str, Any]]:
    try:
        from agents_traces.assemble import assemble_messages
    except ImportError:
        return []
    return assemble_messages(session, limit=limit)


def _record(
    session: str,
    role: str,
    content: str,
    channel: str,
    user: str,
    user_id: str = "",
    project: str = "",
) -> None:
    try:
        from agents_traces.assemble import record_message
    except ImportError:
        return
    record_message(
        session,
        role,
        content,
        channel=channel,
        user=user,
        user_id=user_id,
        project=project,
    )


LOOP_TRAILER_MARKER = "---agents-loop-trailer---"


def _emit_trailer(
    emit: Any,
    *,
    session: str,
    user_id: str,
    alias: str,
    mode: str,
    seal_digest: str = "",
    seal_error: str = "",
) -> None:
    if mode != "buffered":
        return
    data = {"session": session, "user_id": user_id, "alias": alias}
    if seal_digest:
        data["seal"] = seal_digest
    elif seal_error:
        data["seal_error"] = seal_error
    trailer = json.dumps(data, ensure_ascii=False)
    print(f"{LOOP_TRAILER_MARKER}\n{trailer}", file=sys.stdout, flush=True)


def _seal_session(session: str) -> tuple[str, str]:
    """Return ``(digest, error)`` for the session's tool-call hash chain.

    Sealing needs the agents-traces audit API (``agents_traces.audit``).
    Releases without it (0.0.3 and older) still record traces, so report
    why there is no seal instead of dropping it silently.
    """
    try:
        from agents_traces.audit import events_to_records, seal_records
        from agents_traces.store import TraceStore
    except ImportError:
        return "", "agents-traces audit API unavailable (agents_traces.audit missing)"
    try:
        recs = events_to_records(TraceStore().get_events_for_session(session))
        links = seal_records(recs) if recs else []
    except Exception as e:
        return "", f"seal failed: {type(e).__name__}: {e}"
    if not links:
        return "", "no tool calls to seal"
    return links[-1].digest, ""


def _record_tool(
    session: str,
    tool: str,
    args: Any,
    result: Any,
    status: str = "ok",
) -> None:
    if not session:
        return
    try:
        from agents_traces.models import TraceEvent
        from agents_traces.store import TraceStore

        parsed_args = args
        if isinstance(args, str):
            try:
                parsed_args = json.loads(args)
            except Exception:
                parsed_args = {"raw": args}
        elif not isinstance(args, dict):
            parsed_args = {"raw": args}

        event = TraceEvent(
            session=session,
            type="tool_call",
            tool=tool,
            args=parsed_args,
            result=result,
            status=status,
        )
        TraceStore().append(event)
    except Exception:
        pass


def _augment_system(system: str, project: str = "") -> str:
    chunks = [system.strip()] if system and system.strip() else []
    chunks.append(FENCE_SYSTEM_NOTE)
    rules = memory_rules_prompt_block(project)
    if rules:
        chunks.append(rules)
    block = skills_prompt_block()
    if block:
        chunks.append(block)
    return "\n\n".join(chunks)


def _tool_source(name: str, raw_args: Any) -> str:
    if name != "call_job":
        return name or "tool"
    payload: Any = raw_args
    if isinstance(payload, str):
        try:
            payload = json.loads(payload) if payload.strip() else {}
        except json.JSONDecodeError:
            return name
    if isinstance(payload, dict):
        mod_name = str(payload.get("name") or "").strip()
        if mod_name:
            return mod_name
    return name or "tool"


def _tool_call_mutates(name: str, args_str: str) -> bool:
    """Mutator check for in-turn loop protection. Catalog flag wins when the module resolves."""
    if name != "call_job":
        return False
    mod_name = ""
    try:
        parsed = json.loads(args_str) if args_str else {}
    except json.JSONDecodeError:
        parsed = {}
    if isinstance(parsed, dict):
        mod_name = str(parsed.get("name") or "")
    if mod_name:
        mod = find_module(mod_name)
        if mod is not None:
            from .approval import module_is_mutator

            return module_is_mutator(mod)
    return any(tok in args_str for tok in ("write", "add", "remove", "terminal", "delete"))


def _run_tool_calls(
    calls: list[Any],
    *,
    session: str = "",
    default_user: str = "",
    default_channel: str = "",
    default_timezone: str = "",
    on_status: Callable[[str], None] | None = None,
    turn_cache: dict[tuple[str, str], str] | None = None,
) -> list[dict[str, Any]]:
    """Route tool_calls through the three Cordis tools (closed carrier) with loop protection and trace recording."""
    messages: list[dict[str, Any]] = []
    for i, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else call
        name = str(fn.get("name") or call.get("name") or "").strip()
        call_id = str(call.get("id") or f"call_{i}")
        raw_args = fn.get("arguments") or {}

        if on_status:
            args_hint = ""
            if isinstance(raw_args, dict):
                args_hint = str(raw_args.get("name") or raw_args.get("command") or "")
            elif isinstance(raw_args, str) and raw_args.strip():
                try:
                    p = json.loads(raw_args)
                    if isinstance(p, dict):
                        args_hint = str(p.get("name") or p.get("command") or "")
                except Exception:
                    pass
            label = f"{name} ({args_hint[:30]})" if args_hint else name
            on_status(f"running {label}...")

        if is_cordis_tool(name):
            # Loop protection: check identical call in same turn without mutators
            args_str = ""
            if isinstance(raw_args, dict):
                args_str = json.dumps(raw_args, sort_keys=True)
            elif isinstance(raw_args, str):
                args_str = raw_args.strip()

            call_key = (name, args_str)
            is_mutator = _tool_call_mutates(name, args_str)
            source = _tool_source(name, raw_args)

            if turn_cache is not None and not is_mutator and call_key in turn_cache:
                prev_out = turn_cache[call_key]
                content = HarnessMessage(
                    f"[Notice: '{name}' was already called with identical arguments earlier in this turn. "
                    "State has not changed. Previous output:] "
                    + fence_untrusted(source, prev_out[:500])
                )
            else:
                content = handle_cordis_tool(
                    name,
                    call,
                    default_user=default_user,
                    default_channel=default_channel,
                    default_timezone=default_timezone,
                    session=session,
                )
                if turn_cache is not None:
                    if is_mutator:
                        turn_cache.clear()
                    else:
                        turn_cache[call_key] = content
        else:
            content = f"refused: unknown tool {name} (use call_job with a catalog name)"

        _record_tool(
            session=session,
            tool=name,
            args=raw_args,
            result=content,
            status="ok" if not content.startswith("refused") else "error",
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": present_tool_result(source if is_cordis_tool(name) else name, content),
            }
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
    attachments: list[str] | None = None,
    vision: bool = False,
) -> list[dict[str, Any]]:
    """system (cached) → history → clock → new user turn.

    Clock is last-before-user so it cannot invalidate the system prefix.
    """
    system = _augment_system(system, project=project)
    user_content = render_user_content(user_message, attachments, vision=vision)
    try:
        from agents_traces.prompt import PromptParts, clock_message, system_messages
    except ImportError:
        messages: list[dict[str, Any]] = []
        sess_attr = f'id="{session}"'
        if start_date:
            sess_attr += f' start_date="{start_date}"'
        messages.append(
            {
                "role": "system",
                "content": (
                    "<system_prompt>\n"
                    f"  <instructions>\n    <text>{system}</text>\n  </instructions>\n"
                    f"  <runtime_context>\n    <session {sess_attr}/>\n  </runtime_context>\n"
                    "</system_prompt>"
                ),
            }
        )
        messages.extend(_assemble(session, limit=limit))
        if include_clock:
            from datetime import datetime, timezone as utc_tz
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            now = datetime.now(utc_tz.utc)
            if timezone_name:
                try:
                    now = now.astimezone(ZoneInfo(timezone_name))
                except ZoneInfoNotFoundError:
                    pass
            weekday = now.strftime("%A")
            stamp = now.isoformat(timespec="seconds")
            messages.append(
                {"role": "system", "content": f"<clock>{weekday} {stamp}</clock>"}
            )
        if user_content:
            messages.append({"role": "user", "content": user_content})
        return messages

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
    if user_content:
        messages.append({"role": "user", "content": user_content})
    return messages


def _legacy_detached_session(session: str) -> bool:
    """A session id starting with ``routine:`` stays detached for older callers."""
    return (session or "").strip().lower().startswith("routine:")


def _turn_is_detached(args: argparse.Namespace) -> bool:
    """This turn uses its session without replacing the person's active session."""
    if bool(getattr(args, "detached_session", False)):
        return True
    return _legacy_detached_session(getattr(args, "session", None) or "")


def _alias_id(channel: str, user: str) -> str:
    try:
        from agents_traces.identity import alias_id
    except ImportError:
        ch = (channel or "local").strip().lower().replace(" ", "-")
        return f"{ch}:{str(user).strip()}"
    return alias_id(channel, user)


def _lookup_person(ident: Any, *, channel: str, user: str, user_id: str) -> Any:
    person = None
    if user_id and hasattr(ident, "get_user"):
        person = ident.get_user(user_id)
    if person is None and channel and str(user).strip() and hasattr(ident, "find_by_alias"):
        person = ident.find_by_alias(_alias_id(channel, user))
    return person


def _prior_chat_session(ident: Any, *, channel: str, user: str, user_id: str) -> str:
    person = _lookup_person(ident, channel=channel, user=user, user_id=user_id)
    if person is None:
        return ""
    active = str(getattr(person, "active_session", "") or "")
    if _legacy_detached_session(active):
        return ""
    return active


def _restore_chat_session(
    ident: Any,
    *,
    user_id: str,
    explicit_session: str,
    prior_active: str,
    force: bool,
) -> None:
    if not hasattr(ident, "get_user"):
        return
    person = ident.get_user(user_id)
    if person is None:
        return
    current = str(getattr(person, "active_session", "") or "")
    if not force and current != explicit_session and not _legacy_detached_session(current):
        return
    person.active_session = prior_active
    ident.save()


def resolve_identity_store(ident: Any, args: argparse.Namespace) -> Any:
    """Resolve a turn. A detached session is not left as the active session.

    ``--detached-session`` runs in the given session and then restores the
    previous active session. A session id that starts with ``routine:`` does
    the same without the flag.

    agents-traces ``IdentityStore.resolve`` sets ``active_session`` for every
    explicit session. This restores the previous chat session after that write.
    """
    explicit = (getattr(args, "session", None) or "").strip()
    detached = _turn_is_detached(args)
    prior = ""
    if detached:
        prior = _prior_chat_session(
            ident,
            channel=getattr(args, "channel", "") or "",
            user=getattr(args, "user", "") or "",
            user_id=getattr(args, "user_id", "") or "",
        )
    resolved = ident.resolve(
        channel=args.channel,
        user=args.user,
        user_id=args.user_id,
        session=args.session or "",
        new_session=args.new_session,
        project=args.project,
        legacy_exists=_session_has_events(),
    )
    if detached:
        try:
            _restore_chat_session(
                ident,
                user_id=str(getattr(resolved.user, "id", "") or ""),
                explicit_session=explicit,
                prior_active=prior,
                force=bool(getattr(args, "detached_session", False)),
            )
        except Exception as exc:
            print(f"[!] detached session left active: {exc}", file=sys.stderr)
    return resolved


def _session_has_events():
    try:
        from agents_traces.identity import session_has_events
    except ImportError:
        return None
    return session_has_events


def _resolve_identity(args: argparse.Namespace):
    try:
        from agents_traces.identity import IdentityStore
    except ImportError:
        from types import SimpleNamespace

        uid = (args.user_id or args.user or "").strip()
        sid = (args.session or "").strip()
        if not sid:
            sid = "ses_" + uuid.uuid4().hex[:12]
        alias = f"{args.channel}:{args.user}" if args.user else ""
        return SimpleNamespace(
            alias=alias,
            user=SimpleNamespace(
                id=uid,
                display="",
                work="",
                project="",
                timezone="",
                aliases=[],
            ),
            session=SimpleNamespace(id=sid, start_date=""),
        )

    return resolve_identity_store(IdentityStore(), args)


def _complete_once(provider: Any, req: CompletionRequest) -> CompletionResult:
    return _coerce_tool_calls(provider.complete(req))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-request loop: assemble trace, complete, or call a Cordis module"
    )
    parser.add_argument("--session", help="Thread id (ses_…); omit to resume the user's active session")
    parser.add_argument(
        "--detached-session",
        action="store_true",
        help="Use --session for this turn only. Do not store it as the person's active session. A session id starting with routine: is detached even without this flag.",
    )
    parser.add_argument("--channel", default="local", help="Alias scheme (telegram, http, cli, …)")
    parser.add_argument("--user", default="", help="Channel handle (chat id, login, …)")
    parser.add_argument("--user-id", default="", dest="user_id", help="Canonical person id (binds this alias)")
    parser.add_argument("--new-session", action="store_true", help="Start a new thread for this user")
    parser.add_argument("--project", default="", help="Current project for runtime_context")
    parser.add_argument("--message", default="", help="New user turn")
    parser.add_argument(
        "--attach",
        action="append",
        default=None,
        metavar="PATH",
        help="Attach a file (repeatable). Images are vision parts when AGENTS_VISION=1 or the provider advertises vision; otherwise the path is text.",
    )
    parser.add_argument("--limit", type=int, default=24, help="History turns to rebuild")
    parser.add_argument("--system", default="", help="Optional instructions blob (stable prefix)")
    parser.add_argument("--provider", default="", help="Provider name (openai.default, echo, …)")
    parser.add_argument("--persona", default="", help="Persona manifest name (runner/personas/)")
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
    parser.add_argument(
        "--max-tool-rounds",
        type=int,
        default=int(os.environ.get("AGENTS_MAX_TOOL_ROUNDS", "12")),
        help="Cap on tool-call rounds. The last round strips tools and asks for a final answer (default: 12).",
    )
    parser.add_argument(
        "--seal",
        action="store_true",
        help="Cryptographically seal session tool calls and emit seal digest in trailer",
    )
    args = parser.parse_args(argv)
    try:
        from . import __version__
        from .updates import check_for_updates
        check_for_updates("agents-harness", __version__)
    except Exception:
        pass

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

    from .user_profile import load_user_profile

    resolved = _resolve_identity(args)
    session = resolved.session.id
    user_text = args.message or ""
    profile = load_user_profile()
    project = (args.project or "").strip()
    timezone_name = (resolved.user.timezone or profile.get("timezone") or "").strip()

    attachments = [str(item) for item in (args.attach or []) if str(item).strip()]
    provider_manifest = find_provider(args.provider) if args.provider else None
    vision = vision_enabled(provider_manifest)

    system = args.system
    if system:
        from pathlib import Path
        try:
            p = Path(system)
            if p.is_file():
                system = p.read_text(encoding="utf-8")
        except Exception:
            pass

    if args.persona:
        from .personas import persona_system_append

        system = persona_system_append(args.persona, base=system)

    messages = build_payload(
        session,
        user_text,
        limit=args.limit,
        system=system,
        user_id=resolved.user.id,
        user_display=profile.get("display", ""),
        work=profile.get("work", ""),
        project=project,
        timezone_name=timezone_name,
        aliases=resolved.user.aliases,
        start_date=resolved.session.start_date,
        attachments=attachments,
        vision=vision,
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

    if not user_text and not attachments and not args.assemble_only:
        print("Error: --message is required unless --assemble-only with existing history", file=sys.stderr)
        return 1

    provider = get_provider(args.provider)
    mode, on_status, on_delta, emit = bind_delivery(args.channel, override=args.deliver)
    if user_text or attachments:
        recorded = user_text
        if attachments:
            suffix = "\n".join(f"[attach] {path}" for path in attachments)
            recorded = f"{recorded}\n{suffix}".strip() if recorded else suffix
        _record(
            session,
            "user",
            recorded,
            args.channel,
            args.user,
            resolved.user.id,
            project=project,
        )
    tools = openai_tools()
    req = CompletionRequest(
        messages=messages,
        tools=tools or None,
        on_status=on_status,
        on_delta=on_delta,
    )
    max_rounds = max(1, getattr(args, "max_tool_rounds", 12))
    last_scratch = ""
    try:
        result = _complete_once(provider, req)
    except Exception as e:
        err_msg = str(e).strip() or e.__class__.__name__
        print(f"Turn error: {err_msg}", file=sys.stderr)
        emit(f"Turn failed: {err_msg}")
        _emit_trailer(
            emit,
            session=session,
            user_id=resolved.user.id,
            alias=resolved.alias,
            mode=mode,
        )
        return 1

    if (result.text or "").strip():
        last_scratch = result.text.strip()

    turn_cache: dict[tuple[str, str], str] = {}

    for _round in range(max_rounds):
        if not result.tool_calls:
            break
        tool_msgs = _run_tool_calls(
            result.tool_calls,
            session=session,
            default_user=args.user,
            default_channel=args.channel,
            default_timezone=timezone_name,
            on_status=on_status,
            turn_cache=turn_cache,
        )
        messages.append(
            {
                "role": "assistant",
                "content": result.text or None,
                "tool_calls": result.tool_calls,
            }
        )
        messages.extend(tool_msgs)
        is_last = _round >= max_rounds - 1
        round_tools = None if is_last else tools
        if is_last:
            messages.append(
                {
                    "role": "user",
                    "content": "[System Notice: Tool execution round limit reached. Please synthesize your final response now: summarize what was completed, note any tool issues, and answer the user.]",
                }
            )
        try:
            result = _complete_once(
                provider,
                CompletionRequest(
                    messages=messages,
                    tools=round_tools,
                    on_status=on_status,
                    on_delta=on_delta,
                ),
            )
        except Exception as e:
            err_msg = str(e).strip() or e.__class__.__name__
            print(f"Turn error: {err_msg}", file=sys.stderr)
            fallback = last_scratch or f"Turn failed: {err_msg}"
            emit(fallback)
            _emit_trailer(
                emit,
                session=session,
                user_id=resolved.user.id,
                alias=resolved.alias,
                mode=mode,
            )
            return 1

        if (result.text or "").strip():
            last_scratch = result.text.strip()

    if result.tool_calls and not (result.text or "").strip():
        tool_msgs = _run_tool_calls(
            result.tool_calls,
            session=session,
            default_user=args.user,
            default_channel=args.channel,
            default_timezone=timezone_name,
            on_status=on_status,
            turn_cache=turn_cache,
        )
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": result.tool_calls,
            }
        )
        messages.extend(tool_msgs)
        messages.append(
            {
                "role": "user",
                "content": "[System Notice: Provide your final summary to the user now.]",
            }
        )
        try:
            result = _complete_once(
                provider,
                CompletionRequest(
                    messages=messages,
                    tools=None,
                    on_status=on_status,
                    on_delta=on_delta,
                ),
            )
        except Exception as e:
            err_msg = str(e).strip() or e.__class__.__name__
            print(f"Turn error: {err_msg}", file=sys.stderr)
            fallback = last_scratch or f"Turn failed: {err_msg}"
            emit(fallback)
            _emit_trailer(
                emit,
                session=session,
                user_id=resolved.user.id,
                alias=resolved.alias,
                mode=mode,
            )
            return 1

        if (result.text or "").strip():
            last_scratch = result.text.strip()

    final_text = (result.text or "").strip() or last_scratch
    if final_text:
        _record(
            session,
            "assistant",
            final_text,
            args.channel,
            args.user,
            resolved.user.id,
            project=project,
        )

    seal_digest, seal_error = "", ""
    if getattr(args, "seal", False):
        seal_digest, seal_error = _seal_session(session)
        if on_status:
            if seal_digest:
                on_status(f"trace sealed ({seal_digest[:16]}...)")
            else:
                on_status(f"trace not sealed: {seal_error}")

    emit(final_text)
    _emit_trailer(
        emit,
        session=session,
        user_id=resolved.user.id,
        alias=resolved.alias,
        mode=mode,
        seal_digest=seal_digest,
        seal_error=seal_error,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
