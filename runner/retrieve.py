"""Optional content retrieval. The harness does not import a backend.

A host registers a callable, or sets ``AGENTS_CONTENT_READ_CMD``. Traces is
one possible backend: ``read_content(session_id, digest, offset=0, limit=8000)``
matches this signature when those arguments are passed by keyword. Nothing
here imports agents-traces or agents-memory.

Bytes the harness itself clips are stored under the same digest space so a
later ``read_content`` call can page them. A digest that is neither stored
nor answered by a backend is unavailable. The tool says so and does not
invent a body.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

RETRIEVAL_NOTE = (
    "Long tool output is a prefix page, not a sample of both ends. "
    "A page ends with <page offset=\"...\" next_offset=\"...\" total=\"...\" "
    "truncated=\"1\" digest=\"...\"/>. "
    "Call call_job read_content with next true to continue the latest page in this turn. "
    "Or pass offset set to next_offset. "
    "Digest is optional for that latest page. Pass digest when the page tag names an older body. "
    "A negative limit reads through the end. "
    "The result includes next_offset when more text remains. "
    "If read_content says the content is unavailable, say so. "
    "Do not invent or summarize text you have not retrieved."
)

DEFAULT_READ_LIMIT = 32768
TOOL_PREVIEW_CHARS = 4096
TOOL_EVENT_LIMIT = 12

_REF_TAG = re.compile(r"<ref\s+([^>]*?)\s*/?>", re.IGNORECASE)
_PAGE_TAG = re.compile(r"<page\b[^>]*/>", re.IGNORECASE)
_ATTR = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"')
_HEX = frozenset("0123456789abcdef")

ContentBackend = Callable[..., Any]

_backend: ContentBackend | None = None
_memory: dict[str, str] = {}
_cursors: dict[str, dict[str, Any]] = {}


def register_content_backend(backend: ContentBackend | None) -> ContentBackend | None:
    """Register the host reader. ``None`` clears it.

    The callable is ``backend(session_id, digest, offset=0, limit=8000)``.
    Return a mapping or an object with ``text``, ``digest`` (SHA-256 of the
    full body), ``total_chars``, and ``truncated``. ``truncated`` is true when
    ``text`` is not the full body. ``limit < 0`` means read through the end.
    Raise or return ``None`` when the digest is unknown. A ``to_dict()``
    result with those keys is accepted, including a traces ``ContentSlice``.
    """
    global _backend
    _backend = backend
    return backend


def clear_content_backend() -> None:
    register_content_backend(None)


def reset_content_state() -> None:
    """Drop the in-process backend and memory store. Disk files are left as-is."""
    global _backend
    _backend = None
    _memory.clear()
    _cursors.clear()


def content_sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def content_dir() -> Path:
    raw = os.environ.get("AGENTS_CONTENT_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    from agents_workspace import Workspace

    return Workspace.from_env().content


def _normalize_digest(digest: str) -> str:
    cleaned = (digest or "").strip().lower()
    if not cleaned or any(ch not in _HEX for ch in cleaned) or not 12 <= len(cleaned) <= 64:
        raise ValueError("digest must be 12 to 64 hex characters")
    return cleaned


def remember_content(text: str, *, session_id: str = "") -> str:
    """Store the full redacted body and return a ``<ref .../>`` for it."""
    body = "" if text is None else str(text)
    sha = content_sha256(body)
    _memory[sha] = body
    path = content_dir() / sha[:2] / sha
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            tmp = path.with_name(f".{sha}.tmp")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(path)
    except OSError:
        pass
    return format_ref(sha, len(body), session_id=session_id)


def format_ref(sha256: str, chars: int, *, session_id: str = "", digest: str = "") -> str:
    short = (digest or sha256[:12]).strip().lower()
    attrs = [
        f'digest="{short}"',
        f'sha256="{sha256.strip().lower()}"',
        f'chars="{int(chars)}"',
        'truncated="1"',
    ]
    if session_id:
        safe = str(session_id).replace('"', "'").replace("\n", " ")
        attrs.append(f'session="{safe}"')
    return "<ref " + " ".join(attrs) + "/>"


def format_page(
    *,
    offset: int,
    next_offset: int,
    total: int,
    sha256: str,
    session_id: str = "",
) -> str:
    """Continuation address. The shown body is the prefix; this line is extra."""
    short = sha256.strip().lower()[:12]
    attrs = [
        f'offset="{int(offset)}"',
        f'next_offset="{int(next_offset)}"',
        f'total="{int(total)}"',
        'truncated="1"',
        f'digest="{short}"',
        f'sha256="{sha256.strip().lower()}"',
    ]
    if session_id:
        safe = str(session_id).replace('"', "'").replace("\n", " ")
        attrs.append(f'session="{safe}"')
    return "<page " + " ".join(attrs) + "/>"


def page_limit(workspace: Any = None) -> int:
    """``read_content`` page size. Env ``AGENTS_PAGE_CHARS``, else config ``page_chars``."""
    from .settings import int_setting

    return int_setting(
        "AGENTS_PAGE_CHARS",
        "page_chars",
        DEFAULT_READ_LIMIT,
        minimum=1,
        workspace=workspace,
    )


def iter_pages(text: str) -> list[str]:
    return [match.group(0) for match in _PAGE_TAG.finditer(text or "")]


def _track_cursor(session_id: str, sha: str, next_offset: int, total: int) -> None:
    if len(sha) != 64:
        return
    slot = {"sha": sha, "next_offset": int(next_offset), "total": int(total)}
    _cursors[""] = slot
    if session_id:
        _cursors[session_id] = slot


def _cursor_for(session_id: str) -> dict[str, Any] | None:
    if session_id and session_id in _cursors:
        return _cursors[session_id]
    return _cursors.get("")


def parse_ref_attrs(tag: str) -> dict[str, str]:
    match = _REF_TAG.search(tag or "")
    if not match:
        return {}
    return {key: value for key, value in _ATTR.findall(match.group(1))}


def iter_refs(text: str) -> list[str]:
    return [match.group(0) for match in _REF_TAG.finditer(text or "")]


def _local_matches(query: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    blobs = dict(_memory)
    root = content_dir()
    if root.is_dir():
        try:
            paths = list(root.glob("*/*"))
        except OSError:
            paths = []
        for path in paths:
            sha = path.name.strip().lower()
            if len(sha) != 64 or any(ch not in _HEX for ch in sha) or sha in blobs:
                continue
            if len(query) == 64 and sha != query:
                continue
            if len(query) != 64 and not sha.startswith(query):
                continue
            try:
                blobs[sha] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
    for sha, text in blobs.items():
        if text in seen:
            continue
        if len(query) == 64:
            hit = sha == query
        else:
            hit = sha.startswith(query)
        if not hit:
            continue
        if content_sha256(text) != sha:
            continue
        seen.add(text)
        found.append(text)
    return found


def _window(text: str, offset: int, limit: int | None) -> tuple[str, bool]:
    if offset >= len(text):
        piece = ""
    elif limit is None:
        piece = text[offset:]
    else:
        piece = text[offset : offset + limit]
    truncated = offset != 0 or (limit is not None and offset + len(piece) < len(text))
    return piece, truncated


def _as_mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, dict):
            return data
    if hasattr(value, "text"):
        data = {
            "text": getattr(value, "text", ""),
            "digest": getattr(value, "digest", ""),
            "total_chars": getattr(value, "total_chars", None),
            "truncated": getattr(value, "truncated", None),
            "offset": getattr(value, "offset", None),
            "limit": getattr(value, "limit", None),
            "stored_truncated": getattr(value, "stored_truncated", None),
            "digest_short": getattr(value, "digest_short", None),
        }
        return data
    if isinstance(value, str):
        return {"text": value}
    return None


def _unavailable(digest: str, error: str) -> dict[str, Any]:
    return {
        "available": False,
        "error": error,
        "digest": digest,
        "message": (
            "Content for this digest is not available. "
            "Do not invent the missing text."
        ),
    }


def _page(
    text: str,
    *,
    digest: str,
    total_chars: int,
    truncated: bool,
    offset: int,
    limit: int | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    page = {
        "available": True,
        "text": text,
        "digest": digest,
        "total_chars": total_chars,
        "truncated": bool(truncated),
        "offset": offset,
        "limit": limit,
    }
    if extra:
        if extra.get("stored_truncated") is not None:
            page["stored_truncated"] = bool(extra.get("stored_truncated"))
        short = extra.get("digest_short")
        if isinstance(short, str) and short:
            page["digest_short"] = short
    return page


def _from_backend_value(
    value: Any,
    *,
    query: str,
    offset: int,
    limit: int | None,
) -> dict[str, Any] | None:
    data = _as_mapping(value)
    if not data:
        return None
    if data.get("available") is False or data.get("error"):
        return _unavailable(query, str(data.get("error") or "content_unavailable"))
    if "text" not in data:
        return None
    text = "" if data.get("text") is None else str(data.get("text"))
    full = str(data.get("digest") or "").strip().lower()
    total = data.get("total_chars")
    if not isinstance(total, int):
        total = len(text) if data.get("truncated") is not True else None
    truncated = data.get("truncated")
    if truncated is None and isinstance(total, int):
        truncated = offset != 0 or offset + len(text) < total
    if not full:
        full = content_sha256(text) if truncated is not True else ""
    page = _page(
        text,
        digest=full,
        total_chars=int(total) if isinstance(total, int) else len(text),
        truncated=bool(truncated),
        offset=int(data["offset"]) if isinstance(data.get("offset"), int) else offset,
        limit=limit if data.get("limit") is None else data.get("limit"),
        extra=data,
    )
    return page


def _call_registered(
    backend: ContentBackend,
    session_id: str,
    digest: str,
    offset: int,
    limit: int | None,
) -> Any:
    try:
        return backend(session_id, digest, offset=offset, limit=limit)
    except TypeError:
        return backend(session_id, digest, offset, limit)


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", "")
    if isinstance(code, str) and code.strip():
        return code.strip()
    name = type(exc).__name__
    if "NotFound" in name:
        return "not_found"
    if "Mismatch" in name:
        return "digest_mismatch"
    if "Ambiguous" in name:
        return "ambiguous_digest"
    if isinstance(exc, ValueError):
        return "invalid_digest"
    return "content_unavailable"


def _command_backend(session_id: str, digest: str, offset: int, limit: int | None) -> dict[str, Any] | None:
    cmd = os.environ.get("AGENTS_CONTENT_READ_CMD", "").strip()
    if not cmd:
        return None
    import shlex

    # ``posix=True`` eats the backslashes in a Windows path. Split the way the
    # platform quotes, then drop the quotes ``posix=False`` keeps.
    try:
        argv = shlex.split(cmd, posix=(os.name != "nt"))
    except ValueError:
        return _unavailable(digest, "content_unavailable")
    if os.name == "nt":
        argv = [
            item[1:-1] if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'" else item
            for item in argv
        ]
    if not argv:
        return None
    payload = {
        "session_id": session_id,
        "digest": digest,
        "offset": offset,
        "limit": -1 if limit is None else limit,
    }
    try:
        proc = subprocess.run(
            argv,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _unavailable(digest, "content_unavailable")
    if proc.returncode != 0:
        return _unavailable(digest, "content_unavailable")
    try:
        data = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        return _unavailable(digest, "content_unavailable")
    page = _from_backend_value(data, query=digest, offset=offset, limit=limit)
    return page if page is not None else _unavailable(digest, "content_unavailable")


def _resolve_address(
    session_id: str,
    digest: str,
    offset: int | None,
    *,
    next_page: bool,
) -> tuple[str, int] | dict[str, Any]:
    """Pick the body and offset. An empty digest continues the latest page."""
    query = (digest or "").strip().lower()
    slot = _cursor_for(session_id or "")
    if not query:
        if not slot:
            return _unavailable("", "not_found")
        query = str(slot.get("sha") or "")
        if offset is None or next_page:
            offset = int(slot.get("next_offset") or 0)
    elif next_page or offset is None:
        if slot and (str(slot.get("sha") or "") == query or str(slot.get("sha") or "").startswith(query)):
            offset = int(slot.get("next_offset") or 0)
        elif offset is None:
            offset = 0
    if offset is None:
        offset = 0
    return query, offset


class _UsePageDefault:
    """Sentinel: omitted ``limit`` reads ``page_limit()`` at call time."""


_USE_PAGE_DEFAULT = _UsePageDefault()


def _finish_page(session_id: str, page: dict[str, Any]) -> dict[str, Any]:
    if page.get("available"):
        sha = str(page.get("digest") or "")
        offset = page.get("offset") if isinstance(page.get("offset"), int) else 0
        text = "" if page.get("text") is None else str(page.get("text"))
        total = page.get("total_chars") if isinstance(page.get("total_chars"), int) else offset + len(text)
        _track_cursor(session_id, sha, offset + len(text), total)
    return page


def read_content(
    session_id: str,
    digest: str,
    offset: int | None = 0,
    limit: Any = _USE_PAGE_DEFAULT,
    *,
    next_page: bool = False,
) -> dict[str, Any]:
    """Return one page or an unavailable object. Never synthesizes a body.

    Local clips are checked first. Then the registered backend, then
    ``AGENTS_CONTENT_READ_CMD``. ``limit < 0`` or ``None`` reads through the end.
    An empty ``digest`` with ``next_page`` continues the latest page in this process.
    """
    resolved = _resolve_address(session_id or "", digest, offset, next_page=next_page)
    if isinstance(resolved, dict):
        return resolved
    digest, offset = resolved
    if isinstance(limit, _UsePageDefault):
        limit = page_limit()
    if offset < 0:
        return _unavailable(str(digest or ""), "invalid_offset")
    window: int | None
    if limit is None or (isinstance(limit, int) and limit < 0):
        window = None
    else:
        try:
            window = int(limit)
        except (TypeError, ValueError):
            return _unavailable(str(digest or ""), "invalid_limit")
        if window < 0:
            window = None
    try:
        query = _normalize_digest(digest)
    except ValueError:
        return _unavailable(str(digest or ""), "invalid_digest")

    matches = _local_matches(query)
    if len(matches) > 1:
        return _unavailable(query, "ambiguous_digest")
    if len(matches) == 1:
        full_text = matches[0]
        piece, truncated = _window(full_text, offset, window)
        sha = content_sha256(full_text)
        return _finish_page(
            session_id or "",
            _page(
                piece,
                digest=sha,
                total_chars=len(full_text),
                truncated=truncated,
                offset=offset,
                limit=window,
                extra={"digest_short": sha[:12]},
            ),
        )

    if _backend is not None:
        try:
            value = _call_registered(_backend, session_id or "", query, offset, window)
        except Exception as exc:
            return _unavailable(query, _error_code(exc))
        page = _from_backend_value(value, query=query, offset=offset, limit=window)
        if page is not None:
            return _finish_page(session_id or "", page)
        return _unavailable(query, "not_found")

    commanded = _command_backend(session_id or "", query, offset, window)
    if commanded is not None:
        return _finish_page(session_id or "", commanded)
    return _unavailable(query, "not_found")


def render_read_result(page: dict[str, Any]) -> str:
    """Model-facing JSON. Unavailable pages are a harness instruction."""
    from .fence import HarnessError

    if not page.get("available"):
        digest = str(page.get("digest") or "")
        error = str(page.get("error") or "not_found")
        return HarnessError(
            "Content unavailable "
            f"(digest={digest}, error={error}). "
            "Do not invent the missing text."
        )
    shown = {
        "text": page.get("text", ""),
        "digest": page.get("digest", ""),
        "total_chars": page.get("total_chars"),
        "truncated": bool(page.get("truncated")),
        "offset": page.get("offset", 0),
        "limit": page.get("limit"),
    }
    if page.get("digest_short"):
        shown["digest_short"] = page["digest_short"]
    if "stored_truncated" in page:
        shown["stored_truncated"] = bool(page.get("stored_truncated"))
    offset = page.get("offset") if isinstance(page.get("offset"), int) else 0
    text = "" if page.get("text") is None else str(page.get("text"))
    total = page.get("total_chars") if isinstance(page.get("total_chars"), int) else len(text)
    if page.get("truncated") and text and offset + len(text) < total:
        shown["next_offset"] = offset + len(text)
    else:
        shown["next_offset"] = None
    return json.dumps(shown, ensure_ascii=False)


def annotate_clip(text: str, limit: int | None = None, *, session_id: str = "") -> str:
    """Keep a prefix page and append one ``<page .../>`` line when text was removed.

    The page body fits ``limit``. The trailing page line is extra, so the
    returned string can be longer than ``limit`` by that one line. The stored
    body is the full string passed in (already redacted by the caller).
    Continue with ``read_content`` ``next`` true, or ``offset`` = ``next_offset``.
    """
    from .executor import tool_output_max

    body = "" if text is None else str(text)
    cap = tool_output_max() if limit is None else limit
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = tool_output_max()
    if cap < 0:
        cap = tool_output_max()
    if len(body) <= cap:
        return body
    sha = content_sha256(body)
    remember_content(body, session_id=session_id)
    page = body[:cap] if cap > 0 else ""
    _track_cursor(session_id, sha, len(page), len(body))
    tag = format_page(
        offset=0,
        next_offset=len(page),
        total=len(body),
        sha256=sha,
        session_id=session_id,
    )
    if not page:
        return tag
    return page + "\n" + tag


def _preview_limit() -> int:
    from .settings import int_setting

    return int_setting(
        "AGENTS_HISTORY_TOOL_PREVIEW",
        "history_tool_preview",
        TOOL_PREVIEW_CHARS,
        minimum=0,
    )


def _event_limit() -> int:
    from .settings import int_setting

    return int_setting(
        "AGENTS_HISTORY_TOOL_EVENTS",
        "history_tool_events",
        TOOL_EVENT_LIMIT,
        minimum=0,
    )


def _event_meta(event: Any) -> dict[str, Any]:
    meta = getattr(event, "metadata", None)
    return meta if isinstance(meta, dict) else {}


def _preserved_ref(text: str, meta: dict[str, Any], *, session_id: str) -> str:
    pages = iter_pages(text)
    if pages:
        return pages[-1]
    found = iter_refs(text)
    if found:
        return found[-1]
    sha = str(meta.get("result_sha256") or "").strip().lower()
    truncated = bool(meta.get("result_truncated"))
    if not truncated or len(sha) != 64:
        return ""
    digest = str(meta.get("result_digest") or sha[:12]).strip().lower()
    chars = meta.get("result_chars")
    try:
        total = int(chars)
    except (TypeError, ValueError):
        total = 0
    return format_ref(sha, total, session_id=session_id, digest=digest)


def tool_history_block(events: list[Any], *, session_id: str = "") -> str:
    """Bounded recent tool evidence. Longer bodies keep a page address.

    The newest ``AGENTS_HISTORY_TOOL_EVENTS`` calls are included (default 12).
    Each preview is at most ``AGENTS_HISTORY_TOOL_PREVIEW`` characters
    (default 4096). Older calls outside that count are not copied. A body that
    already carries a page or ref tag keeps that tag and is not stored as if
    the preview were the full text.
    """
    limit = _event_limit()
    preview = _preview_limit()
    chosen = list(events or [])
    if limit == 0 or not chosen:
        return ""
    if len(chosen) > limit:
        chosen = chosen[-limit:]
    lines: list[str] = []
    for event in chosen:
        args = getattr(event, "args", None)
        arg_name = args.get("name") if isinstance(args, dict) else ""
        tool = str(getattr(event, "tool", "") or "tool")
        label = f"{tool}({arg_name})" if arg_name else tool
        result = getattr(event, "result", "")
        full = "" if result is None else str(result).strip()
        if not full:
            continue
        meta = _event_meta(event)
        preserved = _preserved_ref(full, meta, session_id=session_id)
        if preserved:
            head = full[:preview]
            line = f"- {label}: {head}"
            if preserved not in head:
                line += "\n  " + preserved
            lines.append(line)
            continue
        if preview >= 0 and len(full) > preview:
            shown = annotate_clip(full, limit=preview, session_id=session_id)
            lines.append(f"- {label}: {shown}")
            continue
        lines.append(f"- {label}: {full}")
    if not lines:
        return ""
    return "<recent_tool_context>\n" + "\n".join(lines) + "\n</recent_tool_context>"
