"""Optional per-call adapter evidence.

A host registers one validator for a ``(tool, adapter, schema, version)`` key.
The call arguments are copied before the tool runs. Only JSON values are
accepted: null, bool, finite int and float, string, array, and object with
string keys. Tuples are accepted and stored as arrays. A set, bytearray,
non-finite number, cycle, non-string key, or other object is rejected before
the tool runs. The error is ``unsupported arguments`` and does not quote the
value. After a successful call, the copy is frozen and passed with that
call's raw result. The validator cannot change the stored arguments or their
digest. Stdout is not scanned. Legacy receipts are a separate path and are
not upgraded by this one. The public envelope omits arguments and digests.

``turn_evidence_scope`` collects invocations for one host turn, through
``final_check``. ``get_current_turn_evidence`` returns that tuple only in the
context that entered the scope. A copied context, asyncio child task, or
worker thread does not own it unless it calls ``adopt_turn_evidence``.
``skill.list``, ``skill.catalog``, and ``skill.load`` do not open an
invocation and are not turn evidence.

Evidence is what the adapter reports about a succeeded call. It is not a host
guarantee of the bytes on disk. A timeout can mean the effect already
happened; this module never reports that the write did not occur.
"""

from __future__ import annotations

import contextvars
import gc
import hashlib
import json
import math
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterator

_MAX_ARGUMENT_DEPTH = 32

OUTCOMES = frozenset({"succeeded", "failed", "denied", "timed_out", "unknown"})
_SHA = re.compile(r"^[0-9a-f]{64}$")


class ArgumentDomainError(ValueError):
    """Arguments are outside the supported JSON domain.

    The message is fixed. It does not include the argument value.
    """

    def __init__(self) -> None:
        super().__init__("unsupported arguments")


Validator = Callable[["CallContext", Any], Any]

_lock = threading.Lock()
_validators: dict[tuple[str, str, str, str], Validator] = {}
_records: dict[str, "InvocationRecord"] = {}


class _TurnLease:
    """One host turn. Copied contexts do not own it."""

    __slots__ = ("records", "tokens", "closed")

    def __init__(self) -> None:
        self.records: list[InvocationRecord] = []
        self.tokens: list[contextvars.Token] = []
        self.closed = False


_turn: contextvars.ContextVar[_TurnLease | None] = contextvars.ContextVar(
    "agents_harness_turn_evidence",
    default=None,
)
_probe: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "agents_harness_turn_context_probe",
    default=False,
)


@dataclass(frozen=True)
class CallContext:
    """Host view of one attempt.

    ``arguments`` is a recursive read-only snapshot of the call, not the
    caller's object. Mappings do not accept writes. Sequences are tuples.
    """

    invocation_id: str
    tool: str
    arguments: Any
    outcome: str


@dataclass(frozen=True)
class AdapterEvidence:
    """Typed evidence copied by the host. Provenance is always ``adapter``."""

    adapter_id: str
    schema: str
    schema_version: str
    operation: str
    target: str
    persisted_sha256: str
    persisted_bytes: int
    provenance: str = "adapter"


@dataclass(frozen=True)
class InvocationRecord:
    """Internal correlation. Does not keep the argument values."""

    invocation_id: str
    tool: str
    args_digest: str
    outcome: str
    evidence: AdapterEvidence | None


def register_result_validator(
    tool: str,
    adapter_id: str,
    schema: str,
    schema_version: str,
    fn: Validator,
) -> Validator:
    """Bind ``fn`` to one tool, adapter, and schema version. The key is not read from tool text."""
    key = (str(tool).strip(), str(adapter_id).strip(), str(schema).strip(), str(schema_version).strip())
    if not all(key):
        raise ValueError("validator binding requires tool, adapter, schema, and version")
    if not callable(fn):
        raise TypeError("validator must be callable")
    with _lock:
        _validators[key] = fn
    return fn


def clear_result_validators() -> None:
    """Drop registrations and invocation records. Tests use this between cases."""
    with _lock:
        _validators.clear()
        _records.clear()


def get_invocation(invocation_id: str) -> InvocationRecord | None:
    """In-process consumer. Returns the stored record, including typed evidence fields."""
    with _lock:
        return _records.get(str(invocation_id))


def public_envelope(invocation_id: str) -> dict[str, Any] | None:
    """Client-safe view.

    Omits arguments, fact text, targets, digests, and errors. ``args_digest``
    stays on the internal record for correlation and is not copied here.
    """
    record = get_invocation(invocation_id)
    if record is None:
        return None
    evidence: dict[str, str] | None = None
    if record.evidence is not None:
        evidence = {
            "provenance": record.evidence.provenance,
            "adapter_id": record.evidence.adapter_id,
            "schema": record.evidence.schema,
            "schema_version": record.evidence.schema_version,
        }
    return {
        "invocation_id": record.invocation_id,
        "tool": record.tool,
        "outcome": record.outcome,
        "evidence": evidence,
    }


def open_invocation(tool: str, arguments: Any, outcome: str, raw: Any) -> InvocationRecord:
    """Record one attempt and, only when it succeeded, ask the single registered validator.

    The digest is the JSON copy taken on entry, before the validator runs.
    Unsupported arguments raise ``ArgumentDomainError`` and store nothing.
    """
    name = str(tool or "")
    state = outcome if outcome in OUTCOMES else "unknown"
    invocation_id = "inv_" + uuid.uuid4().hex
    captured = _snapshot_arguments(arguments)
    digest = _digest_json(captured)
    evidence = _evidence_for(name, _freeze_arguments(captured), state, raw, invocation_id)
    record = InvocationRecord(
        invocation_id=invocation_id,
        tool=name,
        args_digest=digest,
        outcome=state,
        evidence=evidence,
    )
    lease = _owned_lease()
    with _lock:
        _records[invocation_id] = record
        if lease is not None and not lease.closed:
            lease.records.append(record)
    return record


def get_current_turn_evidence() -> tuple[InvocationRecord, ...]:
    """Immutable records for the turn this context owns.

    Empty outside ``turn_evidence_scope``, after it closes, and in a copied
    context, asyncio child task, or worker thread that did not call
    ``adopt_turn_evidence``. The tuple is a snapshot: it is not the live
    buffer and it does not include argument values or the validator registry.
    """
    lease = _owned_lease()
    if lease is None:
        return ()
    with _lock:
        if lease.closed:
            return ()
        return tuple(lease.records)


@contextmanager
def turn_evidence_scope() -> Iterator[None]:
    """Host scope from before tools run through ``final_check``.

    Closes on return, exception, and cancellation. A copied context does not
    become this scope. Workers opt in with ``adopt_turn_evidence``.
    """
    lease = _TurnLease()
    token = _turn.set(lease)
    with _lock:
        lease.tokens.append(token)
    try:
        yield
    finally:
        with _lock:
            lease.closed = True
            lease.records.clear()
            try:
                lease.tokens.remove(token)
            except ValueError:
                pass
        try:
            _turn.reset(token)
        except ValueError:
            pass


@contextmanager
def adopt_turn_evidence() -> Iterator[None]:
    """Attach this context to the turn lease already visible here.

    Context copies, child tasks, and other threads can see the lease object
    and still read and write nothing until they enter this manager. Adopting
    a missing or closed lease is a no-op.
    """
    lease = _turn.get(None)
    if lease is None or lease.closed:
        yield
        return
    token = _turn.set(lease)
    with _lock:
        if lease.closed:
            token_to_drop = token
        else:
            lease.tokens.append(token)
            token_to_drop = None
    if token_to_drop is not None:
        try:
            _turn.reset(token_to_drop)
        except ValueError:
            pass
        yield
        return
    try:
        yield
    finally:
        with _lock:
            try:
                lease.tokens.remove(token)
            except ValueError:
                pass
        try:
            _turn.reset(token)
        except ValueError:
            pass


def _owned_lease() -> _TurnLease | None:
    lease = _turn.get(None)
    if lease is None or lease.closed:
        return None
    here = _current_context()
    if here is None:
        return None
    with _lock:
        tokens = tuple(lease.tokens)
    for token in tokens:
        if _token_context(token) is here:
            return lease
    return None


def _current_context() -> contextvars.Context | None:
    token = _probe.set(True)
    try:
        return _token_context(token)
    finally:
        try:
            _probe.reset(token)
        except ValueError:
            pass


def _token_context(token: contextvars.Token) -> contextvars.Context | None:
    for obj in gc.get_referents(token):
        if isinstance(obj, contextvars.Context):
            return obj
    return None


def _snapshot_arguments(value: Any) -> Any:
    """Detached JSON copy. Tuples become lists. Caller containers are not shared.

    Raises ``ArgumentDomainError`` for anything outside the domain. The
    exception does not chain a value-bearing cause.
    """
    try:
        return _copy_json(value, set(), 0)
    except ArgumentDomainError:
        raise
    except Exception:
        raise ArgumentDomainError from None


def _copy_json(value: Any, stack: set[int], depth: int) -> Any:
    if depth > _MAX_ARGUMENT_DEPTH:
        raise ArgumentDomainError
    if value is None or type(value) is bool or type(value) is str:
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ArgumentDomainError
        return value
    if type(value) is list or type(value) is tuple:
        ident = id(value)
        if ident in stack:
            raise ArgumentDomainError
        stack.add(ident)
        try:
            return [_copy_json(item, stack, depth + 1) for item in value]
        finally:
            stack.discard(ident)
    if type(value) is dict:
        ident = id(value)
        if ident in stack:
            raise ArgumentDomainError
        stack.add(ident)
        try:
            copied: dict[str, Any] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise ArgumentDomainError
                copied[key] = _copy_json(item, stack, depth + 1)
            return copied
        finally:
            stack.discard(ident)
    raise ArgumentDomainError


def _digest_json(value: Any) -> str:
    """SHA-256 of strict JSON. Does not call ``str`` or ``repr`` on values."""
    try:
        blob = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ArgumentDomainError from None
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _freeze_arguments(value: Any) -> Any:
    """Read-only view. Does not alias the containers of ``value``."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_arguments(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_arguments(item) for item in value)
    return value


def _evidence_for(tool: str, arguments: Any, outcome: str, raw: Any, invocation_id: str) -> AdapterEvidence | None:
    if outcome != "succeeded":
        return None
    with _lock:
        matched = [(key, fn) for key, fn in _validators.items() if key[0] == tool]
    if len(matched) != 1:
        return None
    key, fn = matched[0]
    ctx = CallContext(invocation_id=invocation_id, tool=tool, arguments=arguments, outcome=outcome)
    try:
        produced = fn(ctx, raw)
    except Exception:
        return None
    return _accept(key, produced)


def _accept(key: tuple[str, str, str, str], produced: Any) -> AdapterEvidence | None:
    if not isinstance(produced, dict):
        return None
    _tool, adapter_id, schema, version = key
    if "schema" in produced and str(produced.get("schema") or "") != schema:
        return None
    if "schema_version" in produced and str(produced.get("schema_version") or "") != version:
        return None
    if "adapter_id" in produced and str(produced.get("adapter_id") or "") != adapter_id:
        return None
    operation = _text(produced.get("operation"))
    target = _text(produced.get("target"))
    digest = _text(produced.get("persisted_sha256"))
    size = produced.get("persisted_bytes")
    if operation is None or target is None or digest is None or not _SHA.fullmatch(digest):
        return None
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    return AdapterEvidence(
        adapter_id=adapter_id,
        schema=schema,
        schema_version=version,
        operation=operation,
        target=target,
        persisted_sha256=digest,
        persisted_bytes=size,
    )


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None
