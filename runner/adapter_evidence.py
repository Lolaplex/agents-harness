"""Optional per-call adapter evidence.

A host registers one validator for a ``(tool, adapter, schema, version)`` key.
After that tool actually runs, the harness builds an immutable call context and
passes that call's raw result through unchanged. The validator may return
typed evidence or nothing. Stdout is not scanned. Legacy receipts are a
separate path and are not upgraded by this one.

Evidence is what the adapter reports about a succeeded call. It is not a host
guarantee of the bytes on disk. A timeout can mean the effect already
happened; this module never reports that the write did not occur.
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .receipts import args_digest

OUTCOMES = frozenset({"succeeded", "failed", "denied", "timed_out", "unknown"})
_SHA = re.compile(r"^[0-9a-f]{64}$")

Validator = Callable[["CallContext", Any], Any]

_lock = threading.Lock()
_validators: dict[tuple[str, str, str, str], Validator] = {}
_records: dict[str, "InvocationRecord"] = {}


@dataclass(frozen=True)
class CallContext:
    """Host view of one attempt. Arguments stay on this object for the validator."""

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
    """Model- and client-safe view. No arguments, targets, digests, or errors."""
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
        "args_digest": record.args_digest,
        "outcome": record.outcome,
        "evidence": evidence,
    }


def open_invocation(tool: str, arguments: Any, outcome: str, raw: Any) -> InvocationRecord:
    """Record one attempt and, only when it succeeded, ask the single registered validator."""
    name = str(tool or "")
    state = outcome if outcome in OUTCOMES else "unknown"
    invocation_id = "inv_" + uuid.uuid4().hex
    evidence = _evidence_for(name, arguments, state, raw, invocation_id)
    record = InvocationRecord(
        invocation_id=invocation_id,
        tool=name,
        args_digest=args_digest(arguments),
        outcome=state,
        evidence=evidence,
    )
    with _lock:
        _records[invocation_id] = record
    return record


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
