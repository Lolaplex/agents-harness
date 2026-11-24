"""Chat commands on session traces: /undo, /retry, /compress.

Traces are append-only; these ops never rewrite history. An empty-content
``frame`` covers a ts span so the projection in ``assemble_messages`` drops
the covered events while the log keeps them.

- ``/undo`` covers the last turn (last user message and everything after).
- ``/retry`` covers everything after the last user message, so the next
  completion re-answers it.
- ``/compress`` is ``agents_traces.frames.compact_session`` — a compaction
  frame replaces covered events in the projection.
"""

from __future__ import annotations

from typing import Any


def _session_events(session: str, *, days: int = 3650) -> list[Any]:
    from agents_traces.store import TraceStore

    store = TraceStore()
    events = [
        e
        for e in store.iter_events(
            files=store.list_trace_files(days=days), session_id=session
        )
    ]
    events.sort(key=lambda e: e.ts or "")
    return events


def _project_frame(frame: Any) -> None:
    """Mirror the coverage frame into ``<ws>/store.db`` right away.

    Soft imports only — ``klanker.msgsync`` owns the ``msg:*`` projection;
    without klanker (or agents_store) the trace frame alone is the source
    of truth and the next wssync tick projects it anyway. Skipped when the
    trace store isn't the workspace's own (custom ``AGENTS_TRACES_DIR``):
    the projection only mirrors ``<ws>/traces``.
    """
    try:
        from agents_store.db import connect
        from agents_traces.store import TraceStore
        from agents_workspace import Workspace
        from klanker.msgsync import (
            ensure_domain, local_node_id, project_event, project_traces)
        from pathlib import Path

        ws = Workspace.from_env()
        if Path(ws.traces).resolve() != Path(TraceStore().traces_dir).resolve():
            return
        db = getattr(ws, "store_db", None) or ws.dir / "store.db"
        conn = connect(str(db), check_same_thread=False)
        try:
            ensure_domain(conn)
            # Unprojected turn messages must land FIRST: the frame's
            # tombstone span only retires msg entities already in
            # store.db. A message projected after the frame would arrive
            # un-deleted and resurrect the undone turn on synced hosts.
            project_traces(conn, ws.dir, traces_dir=ws.traces)
            project_event(conn, local_node_id(), frame,
                          traces_dir=ws.traces)
        finally:
            conn.close()
    except Exception:
        pass


def _cover(session: str, first_ts: str, last_ts: str, *, kind: str) -> Any:
    """Append an empty-content frame covering [first_ts, last_ts]."""
    from agents_traces.models import TraceEvent
    from agents_traces.store import TraceStore

    frame = TraceEvent(
        session=session,
        type="frame",
        ts=last_ts,
        metadata={
            "kind": kind,
            "summarized_from": {
                "first_ts": first_ts,
                "last_ts": last_ts,
            },
            "content": "",
            "source": "session-op",
        },
    )
    TraceStore().append(frame)
    _project_frame(frame)
    return frame


def _is_frame(event: Any) -> bool:
    return getattr(event, "type", "") == "frame"


def _role(event: Any) -> str:
    md = event.metadata if isinstance(event.metadata, dict) else {}
    return str(md.get("role") or "").lower()


def undo_last_turn(session: str) -> dict[str, Any]:
    """Cover the last user message and everything after it."""
    body = [e for e in _session_events(session) if not _is_frame(e)]
    if not body:
        return {"ok": False, "error": "empty session"}
    start = None
    for i in range(len(body) - 1, -1, -1):
        if body[i].type == "message" and _role(body[i]) == "user":
            start = i
            break
    if start is None:
        return {"ok": False, "error": "no user turn to undo"}
    covered = body[start:]
    _cover(session, covered[0].ts or "", covered[-1].ts or "", kind="undo")
    return {"ok": True, "undone_events": len(covered)}


def rewind_after_last_user(session: str) -> dict[str, Any]:
    """Cover everything after the last user message (for /retry)."""
    body = [e for e in _session_events(session) if not _is_frame(e)]
    if not body:
        return {"ok": False, "error": "empty session"}
    last_user = None
    for i in range(len(body) - 1, -1, -1):
        if body[i].type == "message" and _role(body[i]) == "user":
            last_user = i
            break
    if last_user is None:
        return {"ok": False, "error": "no user turn to retry"}
    covered = body[last_user + 1 :]
    if covered:
        _cover(session, covered[0].ts or "", covered[-1].ts or "", kind="retry")
    return {
        "ok": True,
        "covered_events": len(covered),
        "retry_message": str(
            (body[last_user].metadata or {}).get("content") or ""
        ),
    }


def compress_session(session: str, *, keep_last: int = 6) -> dict[str, Any]:
    """Append a compaction frame over all but the newest keep_last events."""
    from agents_traces.frames import compact_session, frame_meta

    frame = compact_session(session, keep_last=keep_last)
    if frame is None:
        return {"ok": False, "error": "nothing to compress"}
    _project_frame(frame)
    rng = frame_meta(frame).get("summarized_from") or {}
    return {
        "ok": True,
        "covered_events": rng.get("count", 0),
        "content": frame_meta(frame).get("content") or "",
    }
