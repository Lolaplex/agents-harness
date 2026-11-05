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
    "When text contains a <ref digest=\"...\" .../> address or an omission marker "
    "like [... N chars omitted ...], fetch that digest before summarizing or "
    "quoting the missing part. Use call_job with name read_content and arguments "
    "digest, offset, and limit. The next page starts at offset plus the length of "
    "the returned text. If read_content says the content is unavailable, say so. "
    "Do not invent or summarize text you have not retrieved."
)

DEFAULT_READ_LIMIT = 8000
TOOL_PREVIEW_CHARS = 400
TOOL_EVENT_LIMIT = 12

_REF_TAG = re.compile(r"<ref\s+([^>]*?)\s*/?>", re.IGNORECASE)
_ATTR = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"')
_HEX = frozenset("0123456789abcdef")

ContentBackend = Callable[..., Any]

_backend: ContentBackend | None = None
_memory: dict[str, str] = {}


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


def content_sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def content_dir() -> Path:
    raw = os.environ.get("AGENTS_CONTENT_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".agents" / "harness-content"


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

    try:
        argv = shlex.split(cmd, posix=True)
    except ValueError:
        return _unavailable(digest, "content_unavailable")
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


def read_content(
    session_id: str,
    digest: str,
    offset: int = 0,
    limit: int | None = DEFAULT_READ_LIMIT,
) -> dict[str, Any]:
    """Return one page or an unavailable object. Never synthesizes a body.

    Local clips are checked first. Then the registered backend, then
    ``AGENTS_CONTENT_READ_CMD``. ``limit < 0`` or ``None`` reads through the end.
    """
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
        return _page(
            piece,
            digest=sha,
            total_chars=len(full_text),
            truncated=truncated,
            offset=offset,
            limit=window,
            extra={"digest_short": sha[:12]},
        )

    if _backend is not None:
        try:
            value = _call_registered(_backend, session_id or "", query, offset, window)
        except Exception as exc:
            return _unavailable(query, _error_code(exc))
        page = _from_backend_value(value, query=query, offset=offset, limit=window)
        if page is not None:
            return page
        return _unavailable(query, "not_found")

    commanded = _command_backend(session_id or "", query, offset, window)
    if commanded is not None:
        return commanded
    return _unavailable(query, "not_found")


def render_read_result(page: dict[str, Any]) -> str:
    """Model-facing JSON. Unavailable pages are a harness instruction."""
    from .fence import HarnessMessage

    if not page.get("available"):
        digest = str(page.get("digest") or "")
        error = str(page.get("error") or "not_found")
        return HarnessMessage(
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
    return json.dumps(shown, ensure_ascii=False)


def annotate_clip(text: str, limit: int | None = None, *, session_id: str = "") -> str:
    """Clip like ``clip_tool_output`` and append one ref line when text was removed.

    The clipped body fits ``limit``. The trailing ``<ref .../>`` line is extra,
    so the returned string can be longer than ``limit`` by that one line.
    The stored body is the full string passed in (already redacted by the caller).
    """
    from .executor import clip_tool_output, tool_output_max

    body = "" if text is None else str(text)
    cap = tool_output_max() if limit is None else limit
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = tool_output_max()
    clipped = clip_tool_output(body, cap)
    if clipped == body:
        return body
    ref = remember_content(body, session_id=session_id)
    return clipped + "\n" + ref


def _preview_limit() -> int:
    raw = os.environ.get("AGENTS_HISTORY_TOOL_PREVIEW", "").strip()
    if not raw:
        return TOOL_PREVIEW_CHARS
    try:
        value = int(raw)
    except ValueError:
        return TOOL_PREVIEW_CHARS
    return value if value >= 0 else TOOL_PREVIEW_CHARS


def _event_limit() -> int:
    raw = os.environ.get("AGENTS_HISTORY_TOOL_EVENTS", "").strip()
    if not raw:
        return TOOL_EVENT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return TOOL_EVENT_LIMIT
    return value if value >= 0 else TOOL_EVENT_LIMIT


def _event_meta(event: Any) -> dict[str, Any]:
    meta = getattr(event, "metadata", None)
    return meta if isinstance(meta, dict) else {}


def _preserved_ref(text: str, meta: dict[str, Any], *, session_id: str) -> str:
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
    """Bounded recent tool evidence. Longer bodies keep a retrievable ref.

    The newest ``AGENTS_HISTORY_TOOL_EVENTS`` calls are included (default 12).
    Each preview is at most ``AGENTS_HISTORY_TOOL_PREVIEW`` characters
    (default 400). Older calls outside that count are not copied. A body that
    is already a traces prefix keeps that ref and is not stored as if it were
    the full text.
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
            ref = remember_content(full, session_id=session_id)
            lines.append(f"- {label}: {full[:preview]}\n  {ref}")
            continue
        lines.append(f"- {label}: {full}")
    if not lines:
        return ""
    return "<recent_tool_context>\n" + "\n".join(lines) + "\n</recent_tool_context>"
