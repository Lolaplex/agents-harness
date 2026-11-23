"""Persona messaging Cordis verbs: list, inbox, ask, handoff, reply.

``python -m runner.persona_tool <verb>`` is the CLI the ``persona.*`` module
manifests invoke. It rides the ``agents_workspace.inbox`` message schema
(thread_id, hop/max_hops, mode ask|handoff, peer policy) — the executor only
sees exit codes.

Guardrails:
- ``from_persona`` is ``AGENTS_PERSONA`` from the environment, never an arg a
  model can spoof.
- ``AGENTS_PERSONA_HOP`` counts messaging depth; ``ask``/``handoff`` refuse at
  ``AGENTS_PERSONA_MAX_HOPS`` (default 3). A spawned consult turn inherits
  ``hop + 1``.
- Direct self-messages are refused unless ``AGENTS_PERSONA_SELF=1``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import delegate
from .personas import list_personas


def _ws():
    from agents_workspace import Workspace

    return Workspace.from_env()


def _sender() -> str:
    return os.environ.get("AGENTS_PERSONA", "").strip()


def _hop() -> int:
    try:
        return int(os.environ.get("AGENTS_PERSONA_HOP", "0") or 0)
    except ValueError:
        return 0


def _max_hops() -> int:
    try:
        return int(os.environ.get("AGENTS_PERSONA_MAX_HOPS", "3") or 3)
    except ValueError:
        return 3


def _hop_guard() -> None:
    if _hop() >= _max_hops():
        raise SystemExit(_err(f"persona hop limit reached ({_hop()}/{_max_hops()})"))


def _self_guard(to: str) -> None:
    if to == _sender() and os.environ.get("AGENTS_PERSONA_SELF", "") != "1":
        raise SystemExit(_err(f"self-message refused (to={to!r} = AGENTS_PERSONA)"))


def _err(msg: str) -> str:
    print(json.dumps({"ok": False, "error": msg}), file=sys.stderr)
    return msg


def _out(payload: Any) -> int:
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return 0


def _persona_rows(ws) -> list[dict[str, Any]]:
    rows = []
    for row in list_personas(ws):
        meta = {}
        try:
            from agents_workspace.inbox import persona_meta

            meta = persona_meta(ws, str(row.get("id") or ""))
        except Exception:
            pass
        rows.append(
            {
                "id": row.get("id"),
                "name": row.get("name") or row.get("id"),
                "role": meta.get("role") or row.get("role") or "",
                "accepts_messages": meta.get("accepts_messages", True),
                "source": row.get("source") or "",
            }
        )
    return rows


def cmd_list(_args: argparse.Namespace) -> int:
    return _out({"ok": True, "personas": _persona_rows(_ws())})


def cmd_inbox(args: argparse.Namespace) -> int:
    from agents_workspace.inbox import list_inbox

    ws = _ws()
    persona = (args.persona or "").strip() or _sender()
    if not persona:
        raise SystemExit(_err("inbox needs a persona id (arg or AGENTS_PERSONA)"))
    tasks = list_inbox(ws, persona)
    if args.status:
        tasks = [t for t in tasks if t.get("status") == args.status]
    return _out({"ok": True, "persona": persona, "tasks": tasks})


def _spawn_persona_turn(to: str, prompt: str, task_id: str, thread_id: str) -> str:
    """Delegate turn as persona ``to``; hop counter propagates via env."""
    os.environ["AGENTS_PERSONA_HOP"] = str(_hop() + 1)
    brief = (
        f"[persona inbox task {task_id} thread {thread_id} "
        f"from {_sender() or 'system'}] {prompt}"
    )
    spawned = delegate.spawn(brief, persona=to)
    return str(spawned["id"])


def _bind_before_spawn() -> None:
    """Bind the forwarded parent session BEFORE any inbox mutation —
    a bind that fails after ``create_task`` would orphan the task."""
    try:
        delegate.bind_parent_session()
    except delegate.DelegateError as exc:
        raise SystemExit(_err(str(exc)))


def cmd_ask(args: argparse.Namespace) -> int:
    _hop_guard()
    _self_guard(args.to)
    _bind_before_spawn()
    from agents_workspace.inbox import create_task, reply_task

    ws = _ws()
    task = create_task(
        ws,
        args.to,
        args.prompt,
        from_persona=_sender(),
        to_persona=args.to,
        mode="ask",
        hop=_hop(),
        max_hops=_max_hops(),
    )
    dlg_id = _spawn_persona_turn(args.to, args.prompt, task["id"], task["thread_id"])
    state = delegate.result(dlg_id, timeout_sec=args.timeout, tail=50)
    reply = (state.get("stdout") or "").strip()
    if state.get("timed_out") or state.get("state") != "exited" or state.get("exit_code") != 0:
        reply_task(ws, task["id"], status="failed", result=reply or "no reply")
        return _out(
            {
                "ok": False,
                "thread_id": task["thread_id"],
                "task_id": task["id"],
                "delegate": dlg_id,
                "state": state.get("state"),
                "exit_code": state.get("exit_code"),
                "error": "persona turn failed or timed out",
            }
        )
    reply_task(ws, task["id"], result=reply, reply_summary=reply[:200], status="done")
    return _out(
        {
            "ok": True,
            "thread_id": task["thread_id"],
            "task_id": task["id"],
            "delegate": dlg_id,
            "reply": reply,
        }
    )


def cmd_handoff(args: argparse.Namespace) -> int:
    _hop_guard()
    if args.wake:
        _bind_before_spawn()
    from agents_workspace.inbox import create_task, handoff

    ws = _ws()
    if args.task_id:
        task = handoff(ws, args.task_id, args.to, note=args.note or "")
    else:
        if not args.title:
            raise SystemExit(_err("handoff needs --task-id or --title"))
        _self_guard(args.to)
        task = create_task(
            ws,
            args.to,
            args.title,
            note=args.note or "",
            from_persona=_sender(),
            to_persona=args.to,
            mode="handoff",
            hop=_hop(),
            max_hops=_max_hops(),
        )
    out = {
        "ok": True,
        "task_id": task["id"],
        "thread_id": task.get("thread_id"),
        "owner": task.get("owner"),
        "hop": task.get("hop"),
    }
    if args.wake:
        dlg = _spawn_persona_turn(
            args.to, args.title or args.note or f"inbox task {task['id']}",
            task["id"], str(task.get("thread_id") or ""),
        )
        out["wake_delegate"] = dlg
    return _out(out)


def cmd_reply(args: argparse.Namespace) -> int:
    from agents_workspace.inbox import reply_task

    task = reply_task(
        _ws(),
        args.task_id,
        result=args.result or "",
        reply_summary=args.summary or "",
        status=args.status,
    )
    return _out({"ok": True, "task_id": task["id"], "owner": task.get("owner"), "status": task.get("status")})


_HELP_JSON = {
    "name": "persona",
    "description": "Persona discovery and workspace inbox messaging.",
    "commands": {
        "list": {"usage": "list", "side_effects": "read-only"},
        "inbox": {
            "usage": "inbox [PERSONA] [--status STATUS]",
            "side_effects": "read-only",
            "parameters": {
                "type": "object",
                "properties": {
                    "persona": {"type": "string"},
                    "status": {"type": "string", "description": "open|running|done|failed"},
                },
            },
        },
        "ask": {
            "usage": "ask TO PROMPT [--timeout SEC]",
            "side_effects": "mutating",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "prompt": {"type": "string"},
                    "timeout": {"type": "number"},
                },
                "required": ["to", "prompt"],
            },
        },
        "handoff": {
            "usage": "handoff --to PERSONA (--task-id TSK | --title T [--note N]) [--wake]",
            "side_effects": "mutating",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "task_id": {"type": "string"},
                    "title": {"type": "string"},
                    "note": {"type": "string"},
                    "wake": {"type": "boolean"},
                },
                "required": ["to"],
            },
        },
        "reply": {
            "usage": "reply TASK_ID [--result TEXT] [--status STATUS]",
            "side_effects": "mutating",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "result": {"type": "string"},
                    "summary": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["task_id"],
            },
        },
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runner.persona_tool")
    parser.add_argument("--help-json", action="store_true")
    sub = parser.add_subparsers(dest="action")

    sub.add_parser("list").set_defaults(fn=cmd_list)

    ib = sub.add_parser("inbox")
    ib.add_argument("persona", nargs="?", default="")
    ib.add_argument("--status", default="")
    ib.set_defaults(fn=cmd_inbox)

    ak = sub.add_parser("ask")
    ak.add_argument("to")
    ak.add_argument("prompt")
    ak.add_argument("--timeout", type=float, default=300)
    ak.set_defaults(fn=cmd_ask)

    hd = sub.add_parser("handoff")
    hd.add_argument("--to", required=True)
    hd.add_argument("--task-id", dest="task_id", default="")
    hd.add_argument("--title", default="")
    hd.add_argument("--note", default="")
    hd.add_argument("--wake", action="store_true")
    hd.set_defaults(fn=cmd_handoff)

    rp = sub.add_parser("reply")
    rp.add_argument("task_id")
    rp.add_argument("--result", default="")
    rp.add_argument("--summary", default="")
    rp.add_argument("--status", default="done", choices=("done", "failed"))
    rp.set_defaults(fn=cmd_reply)

    args = parser.parse_args(argv)
    if args.help_json:
        return _out(_HELP_JSON)
    fn = getattr(args, "fn", None)
    if fn is None:
        parser.print_help()
        return 2
    try:
        return fn(args)
    except (ValueError, KeyError) as exc:
        raise SystemExit(_err(str(exc)))


if __name__ == "__main__":
    sys.exit(main())
