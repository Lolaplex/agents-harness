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
from .cordis_tools import is_cordis_tool, receipt_for, run_cordis_tool
from .fence import FENCE_SYSTEM_NOTE, HarnessError, HarnessMessage, fence_untrusted, present_tool_result
from .delivery import bind_delivery
from .executor import execute_job, list_schedules
from .modules import (
    find_module,
    list_modules,
    openai_tool_name,
    openai_tools,
)
from .providers import CompletionRequest, CompletionResult, find_provider, get_provider, list_providers
from .pre_pr import PRE_PR_NOTE
from .project_context import project_context_block
from .receipts import GROUNDING_NOTE, final_check_findings, format_receipt_context
from .retrieve import RETRIEVAL_NOTE, iter_refs, tool_history_block
from .search_report import SEARCH_NOTE
from .skills import skills_prompt_block
from .memory_rules import memory_rules_prompt_block
from .pinned import pinned_verbs_prompt_block
from .recall import recall_message, recall_prompt_line


def _assemble(session: str, limit: int) -> list[dict[str, Any]]:
    try:
        from agents_traces.assemble import assemble_messages
    except ImportError:
        return []
    msgs = assemble_messages(session, limit=limit)
    if os.environ.get("AGENTS_HISTORY_TOOLS", "1").strip().lower() not in ("0", "false", "no", "off"):
        try:
            from agents_traces.store import TraceStore
            tool_evts = [
                e for e in TraceStore().get_events_for_session(session)
                if e.type == "tool_call" and e.result
            ]
            block = tool_history_block(tool_evts, session_id=session)
            if block:
                msgs.append({"role": "system", "content": block})
        except Exception:
            pass
    return msgs


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


def _interrupted() -> bool:
    """Relay dropped an interrupt marker at AGENTS_INTERRUPT_FILE."""
    path = os.environ.get("AGENTS_INTERRUPT_FILE", "")
    return bool(path) and os.path.exists(path)


def _emit_trailer(
    emit: Any,
    *,
    session: str,
    user_id: str,
    alias: str,
    mode: str,
    seal_digest: str = "",
    seal_error: str = "",
    response_flags: list[str] | None = None,
    clarify: dict[str, Any] | None = None,
    interrupted: bool = False,
) -> None:
    # Stream delivery used to return here, so /v1/stream never received session.
    _ = mode
    data = {"session": session, "user_id": user_id, "alias": alias}
    if interrupted:
        data["interrupted"] = True
    if seal_digest:
        data["seal"] = seal_digest
    elif seal_error:
        data["seal_error"] = seal_error
    if response_flags:
        data["response_flags"] = list(response_flags)
    if clarify:
        data["clarify"] = clarify
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


def _augment_system(system: str, project: str = "", workspace=None, channel: str = "") -> str:
    chunks = [system.strip()] if system and system.strip() else []
    chunks.append(FENCE_SYSTEM_NOTE)
    chunks.append(RETRIEVAL_NOTE)
    chunks.append(SEARCH_NOTE)
    chunks.append(PRE_PR_NOTE)
    chunks.append(GROUNDING_NOTE)
    rules = memory_rules_prompt_block(project, workspace=workspace)
    if rules:
        chunks.append(rules)
    block = skills_prompt_block(workspace)
    if block:
        chunks.append(block)
    pinned = pinned_verbs_prompt_block(workspace=workspace, channel=channel)
    if pinned:
        chunks.append(pinned)
    line = recall_prompt_line(workspace)
    if line:
        chunks.append(line)
    try:
        project_block = project_context_block(workspace)
    except Exception:
        project_block = ""
    if project_block:
        chunks.append(project_block)
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
    """Mutator check for in-turn loop protection.

    A resolved module uses its metadata. An unresolved ``call_job`` is mutating.
    Argument text is not scanned for words like ``write`` or ``delete``.
    """
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
            from .approval import call_is_mutator
            from .helpjson import with_discovered_commands

            inner = parsed.get("arguments") if isinstance(parsed, dict) else {}
            return call_is_mutator(with_discovered_commands(mod), inner)
    return True


def _replay_snippet(text: str, limit: int = 500) -> str:
    """Keep a trailing ref visible when a repeated read is shortened."""
    if len(text) <= limit:
        return text
    refs = iter_refs(text)
    if not refs:
        return text[:limit]
    ref = refs[-1]
    if len(ref) >= limit:
        return ref[:limit]
    head = text[: max(0, limit - len(ref) - 1)]
    return head + "\n" + ref


def _run_tool_calls(
    calls: list[Any],
    *,
    session: str = "",
    default_user: str = "",
    default_channel: str = "",
    default_timezone: str = "",
    on_status: Callable[[str], None] | None = None,
    turn_cache: dict[tuple[str, str], tuple[str, str]] | None = None,
    receipts: list[dict[str, Any]] | None = None,
    clarifies: list[dict[str, Any]] | None = None,
    workspace: Any = None,
    events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Route tool_calls through the Cordis tools (closed carrier) with loop protection and trace recording."""
    messages: list[dict[str, Any]] = []
    batch: list[dict[str, Any]] = []
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
                prev_out, prev_notice = turn_cache[call_key]
                replay = fence_untrusted(source, _replay_snippet(prev_out))
                if prev_notice:
                    replay = replay + "\n" + prev_notice
                content = HarnessMessage(
                    f"[Notice: '{name}' was already called with identical arguments earlier in this turn. "
                    "State has not changed. Previous output:] "
                    + replay
                )
                notice = ""
            else:
                outcome = run_cordis_tool(
                    name,
                    call,
                    default_user=default_user,
                    default_channel=default_channel,
                    default_timezone=default_timezone,
                    session=session,
                    workspace=workspace,
                )
                content = outcome.text
                notice = outcome.notice
                if outcome.clarify and clarifies is not None:
                    clarifies.append(outcome.clarify)
                receipt = receipt_for(outcome)
                if receipt is not None:
                    batch.append(receipt)
                    if receipts is not None:
                        receipts.append(receipt)
                if events is not None and outcome.mutating:
                    if outcome.success:
                        events.append({"kind": "mutation_success", "tool": outcome.tool})
                    elif not str(content).startswith("Denied:"):
                        # Approval denials are already communicated to the model
                        # with do-not-retry guidance; they are not repairable failure.
                        events.append(
                            {
                                "kind": "mutation_failure",
                                "tool": outcome.tool,
                                "error": str(content)[:160],
                            }
                        )
                if turn_cache is not None:
                    if is_mutator:
                        turn_cache.clear()
                    else:
                        turn_cache[call_key] = (content, notice)
        else:
            content = HarnessError(f"refused: unknown tool {name} (use call_job with a catalog name)")
            notice = ""

        recorded = content if not notice else notice + "\n" + content
        _record_tool(
            session=session,
            tool=name,
            args=raw_args,
            result=recorded,
            status="ok" if not str(content).startswith("refused") else "error",
        )
        presented = present_tool_result(source if is_cordis_tool(name) else name, content)
        if notice:
            presented = presented + "\n" + notice
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": presented,
            }
        )
    if batch:
        messages.append({"role": "user", "content": format_receipt_context(batch)})
    return messages


def _unrepaired_mutation_failures(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Failed mutating calls with no later success of the same tool this turn.

    Module-level matching, not argument-level: a corrected retry almost
    never repeats the failed arguments exactly, and the op-type is the
    intent the model is responsible for. A success only repairs failures
    that came before it — order in the events list is execution order."""
    out: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        if ev.get("kind") != "mutation_failure":
            continue
        repaired = any(
            later.get("kind") == "mutation_success" and later.get("tool") == ev.get("tool")
            for later in events[i + 1 :]
        )
        if not repaired:
            out.append(ev)
    return out


_REPAIR_NOTICE_CAP = 3


def _repair_notice(failures: list[dict[str, Any]]) -> str:
    lines = [
        "[System Notice: {} mutating tool call(s) FAILED this turn and were not corrected:".format(
            len(failures)
        )
    ]
    for ev in failures[:_REPAIR_NOTICE_CAP]:
        lines.append("- {}: {}".format(ev.get("tool") or "?", ev.get("error") or "unknown error"))
    lines.append(
        "Do not report these actions as successful or completed. Either retry with "
        "corrected arguments, or inform the user exactly what failed and what did not happen.]"
    )
    return "\n".join(lines)


_TOOL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_TOOL_FUNC = re.compile(
    r"<tool_call>\s*<function=([A-Za-z0-9_.\-]+)>\s*(.*?)</function>\s*</tool_call>",
    re.DOTALL,
)
_TOOL_PARAM = re.compile(
    r"<parameter=([A-Za-z0-9_.\-]+)>(.*?)</parameter>",
    re.DOTALL,
)
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


def _func_tool_call(name: str, body: str, call_id: str) -> dict[str, Any] | None:
    """qwen heretic variant: <function=NAME><parameter=K>V</parameter>…"""
    name = name.strip()
    if not name:
        return None
    args: dict[str, str] = {}
    for m in _TOOL_PARAM.finditer(body):
        key = m.group(1).strip()
        if key:
            args[key] = m.group(2).strip()
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _parse_text_tool_calls(text: str) -> tuple[list[dict[str, Any]] | None, str]:
    """Local models sometimes emit a tool call as XML/JSON in content."""
    if not text:
        return None, text
    calls: list[dict[str, Any]] = []
    rest = text
    # Collect every <tool_call> block — JSON body or <function=…> body —
    # in document order; leftover prose stays in text.
    while True:
        m_json = _TOOL_TAG.search(rest)
        m_func = _TOOL_FUNC.search(rest)
        match = min((m for m in (m_json, m_func) if m), key=lambda m: m.start(), default=None)
        if match is None:
            break
        if match is m_func:
            parsed = _func_tool_call(match.group(1), match.group(2), f"text_{len(calls)+1}")
        else:
            parsed = _json_tool_call(match.group(1), f"text_{len(calls)+1}")
        rest = (rest[: match.start()] + rest[match.end() :]).strip()
        if parsed is not None:
            calls.append(parsed)
    if calls:
        return calls, rest
    match = _TOOL_FENCE.search(rest)
    if not match:
        return None, text
    parsed = _json_tool_call(match.group(1), "text_1")
    if parsed is None:
        return None, text
    rest = (rest[: match.start()] + rest[match.end() :]).strip()
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


def _skills_for_prompt(workspace=None, channel: str = "") -> list[tuple[str, str]]:
    from .toolsets import filter_modules

    rows = []
    for m in filter_modules(
        list_modules(workspace=workspace), channel=channel, workspace=workspace
    ):
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
    workspace=None,
    channel: str = "",
) -> list[dict[str, Any]]:
    """system (cached) → history → clock → new user turn.

    Clock is last-before-user so it cannot invalidate the system prefix.
    """
    system = _augment_system(system, project=project, workspace=workspace, channel=channel)
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
        notes = recall_message(user_message, workspace=workspace, project=project)
        if notes:
            messages.append({"role": "system", "content": notes})
        if user_content:
            messages.append({"role": "user", "content": user_content})
        return messages

    parts = PromptParts(
        instructions=system,
        skills=_skills_for_prompt(workspace, channel=channel) if include_modules else [],
        user_id=user_id,
        user_display=user_display,
        work=work,
        project=project,
        timezone=timezone_name,
        aliases=list(aliases or []),
        session_id=session,
        start_date=start_date,
    )
    messages: list[dict[str, Any]] = list(
        system_messages(parts, template=_prompt_template(workspace)))
    messages.extend(_assemble(session, limit=limit))
    if include_clock:
        messages.append(clock_message(timezone_name=timezone_name))
    notes = recall_message(user_message, workspace=workspace, project=project)
    if notes:
        messages.append({"role": "system", "content": notes})
    if user_content:
        messages.append({"role": "user", "content": user_content})
    return messages


def _prompt_template(workspace) -> str:
    """Workspace frame override: ``<ws>/prompts/system_prompt.txt`` wins,
    then ``AGENTS_PROMPT_TEMPLATE_FILE``, then inline
    ``AGENTS_PROMPT_TEMPLATE``. Empty means the built-in layout."""
    if workspace is not None:
        try:
            from pathlib import Path

            p = Path(workspace.dir) / "prompts" / "system_prompt.txt"
            if p.is_file():
                return p.read_text(encoding="utf-8")
        except Exception:
            pass
    path = os.environ.get("AGENTS_PROMPT_TEMPLATE_FILE", "").strip()
    if path:
        try:
            from pathlib import Path

            return Path(path).read_text(encoding="utf-8")
        except OSError:
            pass
    return os.environ.get("AGENTS_PROMPT_TEMPLATE", "")


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
    result = provider.complete(req)
    if not req.tools:
        # Terminal rounds (round-cap synthesis, empty-reply nudge) offer no
        # tools: a coerced call could not execute, so coercion would silently
        # eat the only text the model produced. Raw markup in the reply is
        # visible noise; an empty reply is a dead end the user cannot react to.
        return result
    return _coerce_tool_calls(result)


def _main_impl(argv: list[str] | None = None) -> int:
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
    parser.add_argument(
        "--workspace",
        default="",
        help="Data workspace id (default: AGENTS_WORKSPACE). Modules, skills, "
        "schedules, memory rules and traces resolve inside this workspace.",
    )
    parser.add_argument("--message", default="", help="New user turn")
    parser.add_argument(
        "--attach",
        action="append",
        default=None,
        metavar="PATH",
        help="Attach a file (repeatable). Images are vision parts when AGENTS_VISION=1 or the provider advertises vision; otherwise the path is text.",
    )
    parser.add_argument("--limit", type=int, default=int(os.environ.get("AGENTS_HISTORY_LIMIT", "24")), help="History turns to rebuild")
    parser.add_argument("--system", default="", help="Optional instructions blob (stable prefix)")
    parser.add_argument("--provider", default="", help="Provider name (openai.default, echo, …)")
    parser.add_argument("--model", default="", help="Model id override (default: LLM_MODEL env or provider manifest model)")
    parser.add_argument("--persona", default="", help="Persona id (<ws>/personas/<id> first, package fallback; default: AGENTS_PERSONA or the workspace default)")
    parser.add_argument(
        "--toolset",
        default="",
        help="Named toolset (AGENTS_TOOLSET). Restricts list_catalog/load_schema/call_job and the prompt module list to member globs from the config `toolsets` section. `all` clears.",
    )
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

    ws = None
    if args.workspace:
        # CLI entry selection: validate the id, then pin the process env so
        # every downstream resolver call sees the same workspace.
        from agents_workspace import Host

        host = Host.from_env()
        if not host.workspace_exists(args.workspace):
            print(f"Error: unknown workspace: {args.workspace}", file=sys.stderr)
            return 2
        ws = host.workspace(args.workspace)
        os.environ["AGENTS_WORKSPACE"] = ws.id
    from .personas import apply_memory_env, resolve_persona_id
    persona_id = resolve_persona_id(args.persona, workspace=ws)
    os.environ["AGENTS_PERSONA"] = persona_id
    apply_memory_env(persona_id, os.environ, workspace=ws)
    if args.toolset:
        os.environ["AGENTS_TOOLSET"] = args.toolset.strip()

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
        for t in openai_tools(workspace=ws):
            fn = t["function"]
            print(f"  {fn['name']:<24} {fn.get('description', '')}")
        return 0

    if args.list_modules:
        print("modules:")
        for m in list_modules(workspace=ws):
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
        for t in openai_tools(workspace=ws):
            print(f"  {t['function']['name']}")
        return 0

    if args.list_providers:
        print("providers:")
        for p in list_providers():
            print(f"  {p['name']:<20} kind={p['kind']:<14} {p['verb']}")
        return 0

    if args.call:
        mod = find_module(args.call, workspace=ws)
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
    profile = load_user_profile(ws)
    project = (args.project or "").strip()
    # agents-traces 0.1.2 dropped ``UserRecord.timezone``; the profile is the
    # remaining source. ``getattr`` keeps older agents-traces installs working.
    identity_tz = getattr(resolved.user, "timezone", "") or ""
    timezone_name = (identity_tz or profile.get("timezone") or "").strip()

    # Chat commands on session traces. They are ops, not user turns — the
    # command text is not recorded as a message.
    retry_turn = False
    slash, _, slash_arg = user_text.strip().partition(" ")
    slash = slash.lower()
    if slash in ("/undo", "/compress", "/retry"):
        from .session_ops import compress_session, rewind_after_last_user, undo_last_turn

        mode, on_status, on_delta, on_thought, emit = bind_delivery(args.channel, override=args.deliver)
        if slash == "/undo":
            res = undo_last_turn(session)
            text = (
                f"Undone last turn ({res['undone_events']} events)."
                if res.get("ok")
                else f"Nothing to undo: {res.get('error')}."
            )
            emit(text)
            _emit_trailer(emit, session=session, user_id=resolved.user.id, alias=resolved.alias, mode=mode)
            return 0 if res.get("ok") else 1
        if slash == "/compress":
            try:
                keep = int(slash_arg) if slash_arg.strip() else 6
            except ValueError:
                keep = 6
            res = compress_session(session, keep_last=max(1, keep))
            text = (
                f"Compressed {res['covered_events']} earlier events into a context frame."
                if res.get("ok")
                else f"Nothing to compress: {res.get('error')}."
            )
            emit(text)
            _emit_trailer(emit, session=session, user_id=resolved.user.id, alias=resolved.alias, mode=mode)
            return 0 if res.get("ok") else 1
        res = rewind_after_last_user(session)
        if not res.get("ok"):
            emit(f"Nothing to retry: {res.get('error')}.")
            _emit_trailer(emit, session=session, user_id=resolved.user.id, alias=resolved.alias, mode=mode)
            return 1
        retry_turn = True
        user_text = ""

    attachments = [str(item) for item in (args.attach or []) if str(item).strip()]
    provider_name = args.provider
    if not provider_name:
        try:
            from .model import selected_provider_name

            provider_name = os.environ.get("LLM_PROVIDER", "").strip() or selected_provider_name()
        except Exception:
            provider_name = os.environ.get("LLM_PROVIDER", "").strip()
    provider_manifest = find_provider(provider_name) if provider_name else None
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

    if persona_id:
        from .personas import persona_system_append

        system = persona_system_append(persona_id, base=system, workspace=ws)

    _clock_on = os.environ.get("AGENTS_CLOCK", "1").strip().lower() not in (
        "0", "false", "no", "off")
    messages = build_payload(
        session,
        user_text,
        limit=args.limit,
        system=system,
        include_clock=_clock_on,
        user_id=resolved.user.id,
        user_display=profile.get("display", ""),
        work=profile.get("work", ""),
        project=project,
        timezone_name=timezone_name,
        aliases=resolved.user.aliases,
        # AGENTS_CLOCK=0 suppresses every real-time leak for frozen/replay
        # worlds: the <clock> message AND the session start_date stamp.
        start_date=resolved.session.start_date if _clock_on else "",
        attachments=attachments,
        vision=vision,
        workspace=ws,
        channel=args.channel,
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
                    "tools": openai_tools(workspace=ws),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not args.complete:
            return 0

    if not user_text and not attachments and not args.assemble_only and not retry_turn:
        print("Error: --message is required unless --assemble-only with existing history", file=sys.stderr)
        return 1

    provider = get_provider(args.provider or provider_name)
    mode, on_status, on_delta, on_thought, emit = bind_delivery(args.channel, override=args.deliver)
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
    tools = openai_tools(workspace=ws)
    model_override = args.model
    if not model_override:
        try:
            from .model import selected_model

            model_override = selected_model()
        except Exception:
            model_override = ""
    req = CompletionRequest(
        model=model_override,
        messages=messages,
        tools=tools or None,
        on_status=on_status,
        on_delta=on_delta,
        on_thought=on_thought,
    )
    max_rounds = max(1, getattr(args, "max_tool_rounds", 12))
    last_scratch = ""
    if _interrupted():
        print("[!] interrupted", file=sys.stderr)
        _emit_trailer(
            emit,
            session=session,
            user_id=resolved.user.id,
            alias=resolved.alias,
            mode=mode,
            interrupted=True,
        )
        return 0
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

    turn_cache: dict[tuple[str, str], tuple[str, str]] = {}
    turn_receipts: list[dict[str, Any]] = []
    turn_clarifies: list[dict[str, Any]] = []
    turn_events: list[dict[str, Any]] = []
    repair_noticed: set[tuple[Any, Any]] = set()
    repair_attempts = 0
    try:
        repair_max = int(os.environ.get("AGENTS_REPAIR_MAX", "2"))
    except ValueError:
        repair_max = 2
    repair_enabled = os.environ.get("AGENTS_REPAIR_FAILED_MUTATIONS", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }

    for _round in range(max_rounds):
        if _interrupted():
            print("[!] interrupted", file=sys.stderr)
            _emit_trailer(
                emit,
                session=session,
                user_id=resolved.user.id,
                alias=resolved.alias,
                mode=mode,
                interrupted=True,
            )
            return 0
        if not result.tool_calls:
            # Truthfulness invariant: a turn may not end while a mutating
            # call this turn failed and was neither repaired nor disclosed.
            # Each distinct failure is named exactly once — a genuinely
            # impossible op must not be nagged about repeatedly, but the
            # model always gets one explicit chance to correct or disclose.
            if repair_enabled and turn_events:
                unrepaired = [
                    ev
                    for ev in _unrepaired_mutation_failures(turn_events)
                    if (ev.get("tool"), ev.get("error")) not in repair_noticed
                ]
                if unrepaired and repair_attempts < repair_max:
                    repair_attempts += 1
                    for ev in unrepaired:
                        repair_noticed.add((ev.get("tool"), ev.get("error")))
                    messages.append({"role": "assistant", "content": result.text or None})
                    messages.append({"role": "user", "content": _repair_notice(unrepaired)})
                    try:
                        result = _complete_once(
                            provider,
                            CompletionRequest(
                                model=model_override,
                                messages=messages,
                                tools=tools,
                                on_status=on_status,
                                on_delta=on_delta,
                                on_thought=on_thought,
                            ),
                        )
                    except Exception:
                        break
                    if (result.text or "").strip():
                        last_scratch = result.text.strip()
                    continue
            break
        tool_msgs = _run_tool_calls(
            result.tool_calls,
            session=session,
            default_user=args.user,
            default_channel=args.channel,
            default_timezone=timezone_name,
            on_status=on_status,
            turn_cache=turn_cache,
            receipts=turn_receipts,
            clarifies=turn_clarifies,
            workspace=ws,
            events=turn_events,
        )
        messages.append(
            {
                "role": "assistant",
                "content": result.text or None,
                "tool_calls": result.tool_calls,
            }
        )
        messages.extend(tool_msgs)
        if _interrupted():
            print("[!] interrupted", file=sys.stderr)
            _emit_trailer(
                emit,
                session=session,
                user_id=resolved.user.id,
                alias=resolved.alias,
                mode=mode,
                interrupted=True,
            )
            return 0
        if turn_clarifies:
            break
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
                    model=model_override,
                    messages=messages,
                    tools=round_tools,
                    on_status=on_status,
                    on_delta=on_delta,
                    on_thought=on_thought,
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
            receipts=turn_receipts,
            workspace=ws,
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
                    model=model_override,
                    messages=messages,
                    tools=None,
                    on_status=on_status,
                    on_delta=on_delta,
                    on_thought=on_thought,
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
    if not final_text and not turn_clarifies:
        # Reasoning models can end a turn on reasoning alone — empty content,
        # no tool calls. Ask once for the visible reply before giving up.
        messages.append(
            {
                "role": "user",
                "content": "[System Notice: Respond to the user now with your answer.]",
            }
        )
        try:
            result = _complete_once(
                provider,
                CompletionRequest(
                    model=model_override,
                    messages=messages,
                    tools=None,
                    on_status=on_status,
                    on_delta=on_delta,
                    on_thought=on_thought,
                ),
            )
        except Exception:
            result = None
        if result is not None and (result.text or "").strip():
            final_text = result.text.strip()
    response_flags: list[str] = []
    if turn_clarifies:
        # The turn ends with the question; channels render options where they can.
        ask = turn_clarifies[-1]
        lines = [ask["question"]]
        for i, opt in enumerate(ask.get("options") or [], 1):
            lines.append(f"{i}. {opt}")
        final_text = "\n".join(lines)
        response_flags = []
    if final_text and not turn_clarifies:
        findings = final_check_findings(final_text, turn_receipts)
        if findings:
            messages.append({"role": "assistant", "content": final_text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "[Harness check] "
                        + " ".join(findings)
                        + " Revise the reply. Do not claim a side effect that has no successful receipt in this turn."
                    ),
                }
            )
            try:
                revised = _complete_once(
                    provider,
                    CompletionRequest(
                        model=model_override,
                        messages=messages,
                        tools=None,
                        on_status=on_status,
                        on_delta=on_delta,
                        on_thought=on_thought,
                    ),
                )
            except Exception as exc:
                err_msg = str(exc).strip() or exc.__class__.__name__
                print(f"Final check revision failed: {err_msg}", file=sys.stderr)
                revised = None
            if revised is not None and (revised.text or "").strip():
                final_text = revised.text.strip()
            response_flags = final_check_findings(final_text, turn_receipts)
            if response_flags:
                print(
                    "response flagged: " + "; ".join(response_flags),
                    file=sys.stderr,
                )
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
        response_flags=response_flags,
        clarify=turn_clarifies[-1] if turn_clarifies else None,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the loop, restoring host-level env vars it sets for children."""
    saved = {
        k: os.environ.get(k)
        for k in ("AGENTS_PERSONA", "AGENTS_WORKSPACE", "AGENTS_TOOLSET")
    }
    try:
        return _main_impl(argv)
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


if __name__ == "__main__":
    sys.exit(main())
