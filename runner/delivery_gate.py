"""Host delivery decision and the terminal envelope.

The model does not choose delivery status. A registered hook may allow,
replace, or block the candidate after ``final_check``. ``--delivery-required``
makes stdout exactly one ``agents_harness.delivery.v1`` object.

This module does not redact tool traces. A trace tail is still a trace tail.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .receipts import final_check_outcome, snapshot_receipts

SCHEMA = "agents_harness.delivery.v1"
STATUSES = frozenset({"released", "blocked", "policy_failure", "cancelled", "timed_out"})
STDOUT_CAP = 262144
MAX_DEPTH = 8
MAX_NODES = 32
_ACTIONS = frozenset({"allow", "replace", "block"})
_FIELDS = frozenset({"schema", "status", "reply", "session", "user_id", "alias"})

DeliveryHook = Callable[[str, list[dict[str, Any]]], Any]

_hook: DeliveryHook | None = None
_cancel = threading.Event()
_committed = False


@dataclass(frozen=True)
class DeliveryDecision:
    """``allow`` releases the candidate. ``replace`` releases ``text``. ``block`` releases nothing."""

    action: str
    text: str = ""


class DeliveryStop(Exception):
    """Internal stop for a gated turn. The message is the kind, never the candidate."""

    def __init__(self, kind: str, candidate: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.candidate = candidate if isinstance(candidate, str) else ""


def register_delivery_decision(hook: DeliveryHook | None) -> DeliveryHook | None:
    """Register the host decision. ``None`` clears it. The hook is not a tool."""

    global _hook
    _hook = hook
    return hook


def clear_delivery_decision() -> None:
    register_delivery_decision(None)


def current_delivery_decision() -> DeliveryHook | None:
    return _hook


def request_turn_cancel() -> None:
    """Ask the current turn to stop at the next phase boundary. Does not retry."""

    _cancel.set()


def turn_cancelled() -> bool:
    return _cancel.is_set()


def reset_turn_cancel() -> None:
    _cancel.clear()


def terminal_committed() -> bool:
    return _committed


def reset_terminal_commit() -> None:
    global _committed
    _committed = False


def begin_turn_scope() -> None:
    reset_turn_cancel()
    reset_terminal_commit()


def end_turn_scope() -> None:
    reset_turn_cancel()
    reset_terminal_commit()


def unsafe_text(value: Any) -> bool:
    """Reject non-strings, C0 controls other than tab/LF/CR, DEL, and surrogates."""

    if not isinstance(value, str):
        return True
    for ch in value:
        code = ord(ch)
        if code == 0x7F or (code < 32 and ch not in "\t\n\r"):
            return True
        if 0xD800 <= code <= 0xDFFF:
            return True
    return False


def decision_to_status(decision: Any, candidate: str) -> tuple[str, str]:
    """Map a hook result to ``(status, reply)``. Invalid results fail closed."""

    if not isinstance(decision, DeliveryDecision):
        return "policy_failure", ""
    action = decision.action
    if not isinstance(action, str) or action not in _ACTIONS:
        return "policy_failure", ""
    if not isinstance(decision.text, str):
        return "policy_failure", ""
    if action == "block":
        return "blocked", ""
    reply = candidate if action == "allow" else decision.text
    if unsafe_text(reply):
        return "policy_failure", ""
    return "released", reply


def _apply_policy(hook: DeliveryHook | None, text: str, receipts: list[dict[str, Any]]) -> tuple[str, str]:
    if turn_cancelled():
        return "cancelled", ""
    if hook is None:
        return "policy_failure", ""
    try:
        decision = hook(text, snapshot_receipts(receipts))
    except Exception:
        return "policy_failure", ""
    if turn_cancelled():
        return "cancelled", ""
    return decision_to_status(decision, text)


def resolve_terminal(
    *,
    kind: str,
    candidate: str,
    receipts: list[dict[str, Any]],
    hook: DeliveryHook | None,
    revise: Callable[[str, list[str]], str],
) -> tuple[str, str]:
    """One check, at most one revision, then one policy call.

    A check exception skips revision and policy. Provider failures call policy
    once with an empty candidate. Cancel before policy skips the hook. Cancel
    after the hook drops its text. Nothing here starts another tool round.
    """

    if kind == "cancelled" or turn_cancelled():
        return "cancelled", ""
    if kind == "provider":
        return _apply_policy(hook, "", receipts)
    findings, err = final_check_outcome(candidate, receipts)
    if turn_cancelled():
        return "cancelled", ""
    if err is not None:
        return "policy_failure", ""
    text = candidate
    if findings:
        try:
            revised = revise(text, findings)
        except Exception:
            revised = ""
        if turn_cancelled():
            return "cancelled", ""
        text = revised if isinstance(revised, str) else ""
    return _apply_policy(hook, text, receipts)


def encode_envelope(
    status: str,
    reply: str,
    session: str,
    user_id: str,
    alias: str,
) -> bytes | None:
    """Canonical UTF-8 object plus one LF, or ``None`` when it must not be written."""

    if status not in STATUSES or not isinstance(reply, str):
        return None
    if status != "released" and reply != "":
        return None
    if any(unsafe_text(part) for part in (reply, session, user_id, alias, status)):
        return None
    payload = {
        "alias": alias,
        "reply": reply,
        "schema": SCHEMA,
        "session": session,
        "status": status,
        "user_id": user_id,
    }
    try:
        body = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return None
    data = body + b"\n"
    if len(data) > STDOUT_CAP:
        return None
    return data


def _reject_constant(_name: str) -> None:
    raise ValueError("nonfinite")


def _pairs(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _walk(node: Any, depth: int, nodes: list[int]) -> None:
    nodes[0] += 1
    if nodes[0] > MAX_NODES:
        raise ValueError("nodes")
    if depth > MAX_DEPTH:
        raise ValueError("depth")
    if isinstance(node, dict):
        for key, value in node.items():
            if unsafe_text(key):
                raise ValueError("key")
            _walk(value, depth + 1, nodes)
        return
    if isinstance(node, list):
        for item in node:
            _walk(item, depth + 1, nodes)
        return
    if isinstance(node, str):
        if unsafe_text(node):
            raise ValueError("text")
        return
    if node is None or isinstance(node, bool) or isinstance(node, int):
        return
    if isinstance(node, float):
        raise ValueError("float")
    raise ValueError("type")


def parse_delivery_envelope(raw: bytes) -> dict[str, str]:
    """Strict terminal object. Raises ``ValueError`` on every reject path.

    Accepts the bytes with no trailing newline, or with exactly one trailing LF.
    Duplicate keys are rejected after JSON unescape. The reply string is data.
    """

    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("type")
    blob = bytes(raw)
    if len(blob) > STDOUT_CAP or not blob:
        raise ValueError("size")
    if blob.endswith(b"\n"):
        body = blob[:-1]
        if body.endswith(b"\n"):
            raise ValueError("trailing")
    else:
        body = blob
    if b"\n" in body or b"\r" in body or not body:
        raise ValueError("framing")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("utf-8") from exc
    if not text.startswith("{") or text[:1].isspace():
        raise ValueError("root")
    decoder = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_reject_constant)
    try:
        obj, end = decoder.raw_decode(text)
    except (json.JSONDecodeError, ValueError, UnicodeError) as exc:
        raise ValueError("json") from exc
    if text[end:] != "":
        raise ValueError("trailing")
    if not isinstance(obj, dict):
        raise ValueError("root")
    _walk(obj, 1, [0])
    if set(obj) != _FIELDS:
        raise ValueError("fields")
    values: dict[str, str] = {}
    for key in _FIELDS:
        value = obj[key]
        if not isinstance(value, str) or unsafe_text(value):
            raise ValueError("field type")
        values[key] = value
    if values["schema"] != SCHEMA or values["status"] not in STATUSES:
        raise ValueError("status")
    if values["status"] != "released" and values["reply"] != "":
        raise ValueError("consistency")
    return values


TRANSPORT = 3


def _write_bytes(data: bytes) -> int:
    """Hand ``data`` to stdout. This function does not read the cancel flag.

    The caller already decided the body. A cancel that becomes visible on the
    first line here, or during ``write`` or ``flush``, does not select a
    different body and does not retract bytes already handed to the stream.
    A ``write`` or ``flush`` error is a transport failure. It is not rewritten
    as ``blocked``.
    """
    global _committed
    if _committed:
        return 0
    buf = getattr(sys.stdout, "buffer", None)
    try:
        if buf is not None:
            buf.write(data)
        else:
            sys.stdout.write(data.decode("utf-8"))
    except Exception:
        _committed = True
        return TRANSPORT
    try:
        if buf is not None:
            buf.flush()
        else:
            sys.stdout.flush()
    except Exception:
        _committed = True
        return TRANSPORT
    _committed = True
    return 0


def write_terminal(
    *,
    required: bool,
    status: str,
    reply: str,
    session: str,
    user_id: str,
    alias: str,
    record: Callable[[str], None],
) -> int:
    """Write the terminal bytes once.

    The release commit point is the last ``turn_cancelled()`` read below,
    before the first stdout write. Cancel at that read selects ``cancelled``
    and an empty reply, so the candidate is not released. ``_write_bytes``
    does not read the flag. A cancel after this read, including one made on
    the first line of the write, does not replace the body and does not
    retract bytes. A short write or a flush error returns ``TRANSPORT`` (3).
    That is not a later ``blocked`` envelope. Already finished tool side
    effects are left as they are. Oversize required output writes nothing
    and returns 2. There is no atomic pipe or network rollback.
    """

    global _committed
    if _committed:
        return 0
    if turn_cancelled():
        status, reply = "cancelled", ""
    if not isinstance(reply, str):
        status, reply = "policy_failure", ""
    if required:
        if turn_cancelled():
            status, reply = "cancelled", ""
        blob = encode_envelope(status, reply, session, user_id, alias)
        if blob is None:
            return 2
        if turn_cancelled():
            status, reply = "cancelled", ""
            blob = encode_envelope(status, reply, session, user_id, alias)
            if blob is None:
                return 2
        code = _write_bytes(blob)
        if code == TRANSPORT:
            return TRANSPORT
        if status != "released":
            print("delivery withheld", file=sys.stderr)
        if status == "released" and reply != "":
            record(reply)
        return 0
    if turn_cancelled():
        _committed = True
        print("delivery withheld", file=sys.stderr)
        return 0
    if status == "released" and reply != "":
        code = _write_bytes(reply.encode("utf-8"))
        if code == TRANSPORT:
            return TRANSPORT
        record(reply)
        return 0
    _committed = True
    if status != "released":
        print("delivery withheld", file=sys.stderr)
    return 0
