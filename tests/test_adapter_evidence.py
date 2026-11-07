"""Opt-in adapter evidence is bound to one real call, not to stdout JSON.

The synthetic writer below performs the write and the validator reads those
bytes back. Neither one is a memory package, and neither one scans stdout.
"""

from __future__ import annotations

import hashlib
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.adapter_evidence import (
    clear_result_validators,
    get_invocation,
    public_envelope,
    register_result_validator,
)
from runner.cordis_tools import receipt_for, run_cordis_tool
from runner.mcp_client import RemoteToolError
from runner.receipts import format_receipt_context

TOOL = "example.write"
ADAPTER = "synthetic"
SCHEMA = "example.write_result"
VERSION = "1"
SECRET = "SUPER_SECRET_FACT"
OTHER = "other-target"


def _module() -> dict:
    return {
        "name": TOOL,
        "kind": "mcp",
        "when": "on_request",
        "cadence": "on_request",
        "verb": "python -m example_write",
        "rests_on": "synthetic",
        "expected_exit": 0,
        "mutates": True,
    }


def _call(dest: str, fact: str = SECRET) -> dict:
    return {
        "function": {
            "arguments": json.dumps(
                {"name": TOOL, "arguments": {"dest": dest, "fact": fact}}
            )
        }
    }


def _decoys() -> str:
    orange = {
        "schema": "example.write_receipt.v1",
        "id": "notes/orange",
        "verified": True,
        "sha256": "ab" * 32,
    }
    violet = {
        "schema": "example.write_receipt.v1",
        "id": "notes/violet",
        "verified": False,
        "sha256": "cd" * 32,
    }
    return json.dumps(orange) + "\n" + json.dumps(violet)


def _transformed(fact: str) -> bytes:
    return (fact.upper() + "\n").encode("utf-8")


def _job(path: Path, fact: str, *, status: str = "SUCCESS", exit_code: int = 0, write: bool = True) -> dict:
    if write:
        path.write_bytes(_transformed(fact))
    return {
        "status": status,
        "exit_code": exit_code,
        "stdout": _decoys(),
        "stderr": "",
    }


def _readback(ctx, _result) -> dict:
    raw = Path(ctx.arguments["dest"]).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "operation": "create",
        "target": "gen_" + digest[:12],
        "persisted_sha256": digest,
        "persisted_bytes": len(raw),
    }


def _run(path: Path, fact: str = SECRET, **job) -> object:
    record = _job(path, fact, **job)

    def execute(mod, extra_argv=None):
        return record

    with patch("runner.cordis_tools.find_module", return_value=_module()):
        with patch("runner.cordis_tools.execute_job", side_effect=execute):
            return run_cordis_tool("call_job", _call(str(path), fact))


class TestAdapterEvidence(unittest.TestCase):
    def setUp(self) -> None:
        clear_result_validators()
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        clear_result_validators()
        self._tmp.cleanup()

    def _register(self, fn=_readback, tool: str = TOOL) -> None:
        register_result_validator(tool, ADAPTER, SCHEMA, VERSION, fn)

    def test_create_readback_is_bound_to_this_invocation(self) -> None:
        self._register()
        path = self.root / "one"
        outcome = _run(path)
        record = get_invocation(outcome.invocation_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.outcome, "succeeded")
        self.assertTrue(outcome.success)
        evidence = record.evidence
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence.provenance, "adapter")
        self.assertEqual(evidence.operation, "create")
        raw = _transformed(SECRET)
        self.assertNotEqual(evidence.target, "notes/orange")
        self.assertNotEqual(evidence.target, OTHER)
        self.assertTrue(evidence.target.startswith("gen_"))
        self.assertEqual(evidence.persisted_sha256, hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(evidence.persisted_sha256, record.args_digest)
        self.assertNotEqual(evidence.persisted_bytes, len(SECRET.encode()))
        legacy = receipt_for(outcome)
        self.assertIsNotNone(legacy)
        assert legacy is not None
        self.assertEqual(legacy["receipt"]["id"], "notes/orange")
        self.assertNotIn("evidence", legacy)
        shown = json.dumps(public_envelope(outcome.invocation_id))
        self.assertNotIn(SECRET, shown)
        self.assertNotIn(evidence.target, shown)
        self.assertNotIn(evidence.persisted_sha256, shown)
        self.assertNotIn(str(path), shown)
        self.assertNotIn(SECRET, format_receipt_context([legacy]))

    def test_stdout_decoys_are_not_authorized_evidence(self) -> None:
        path = self.root / "decoy"
        outcome = _run(path, write=False)
        record = get_invocation(outcome.invocation_id)
        assert record is not None
        self.assertIsNone(record.evidence)
        self.assertEqual(receipt_for(outcome)["receipt"]["id"], "notes/orange")
        self.assertIsNone(public_envelope(outcome.invocation_id)["evidence"])

    def test_parallel_same_args_are_distinct_attempts(self) -> None:
        self._register()
        path = self.root / "parallel"
        body = _transformed(SECRET)
        gate = threading.Lock()

        def execute(mod, extra_argv=None):
            with gate:
                path.write_bytes(body)
            return _job(path, SECRET, write=False)

        outcomes = []

        def once() -> None:
            with patch("runner.cordis_tools.find_module", return_value=_module()):
                with patch("runner.cordis_tools.execute_job", side_effect=execute):
                    outcomes.append(run_cordis_tool("call_job", _call(str(path))))

        threads = [threading.Thread(target=once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        ids = [item.invocation_id for item in outcomes]
        self.assertEqual(len(set(ids)), 2)
        records = [get_invocation(item) for item in ids]
        self.assertEqual(records[0].args_digest, records[1].args_digest)
        self.assertIsNot(records[0], records[1])
        self.assertEqual(records[0].evidence.persisted_sha256, records[1].evidence.persisted_sha256)

    def test_retry_and_replay_do_not_reuse_invocation_or_old_digest(self) -> None:
        self._register()
        path = self.root / "retry"
        first = _run(path, "alpha")
        second = _run(path, "beta")
        old = get_invocation(first.invocation_id)
        new = get_invocation(second.invocation_id)
        assert old is not None and new is not None and old.evidence and new.evidence
        self.assertNotEqual(first.invocation_id, second.invocation_id)
        self.assertNotEqual(old.evidence.persisted_sha256, new.evidence.persisted_sha256)
        self.assertEqual(old.evidence.persisted_sha256, hashlib.sha256(_transformed("alpha")).hexdigest())
        self.assertEqual(get_invocation(first.invocation_id).evidence.target, old.evidence.target)

    def test_foreign_tool_adapter_or_schema_is_not_evidence(self) -> None:
        register_result_validator("other.tool", ADAPTER, SCHEMA, VERSION, _readback)
        path = self.root / "foreign"
        outcome = _run(path)
        self.assertIsNone(get_invocation(outcome.invocation_id).evidence)
        clear_result_validators()
        self._register()
        register_result_validator(TOOL, "second", SCHEMA, "2", _readback)
        outcome = _run(self.root / "ambiguous")
        self.assertEqual(outcome.call_outcome, "succeeded")
        self.assertIsNone(get_invocation(outcome.invocation_id).evidence)

    def test_incomplete_or_crashing_validator_leaves_success_without_evidence(self) -> None:
        def incomplete(ctx, result):
            return {"operation": "create"}

        self._register(incomplete)
        path = self.root / "missing"
        outcome = _run(path)
        self.assertTrue(outcome.success)
        self.assertIsNone(get_invocation(outcome.invocation_id).evidence)
        clear_result_validators()

        def boom(ctx, result):
            raise RuntimeError("SECRET_BOOM")

        self._register(boom)
        outcome = _run(self.root / "boom")
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.call_outcome, "succeeded")
        shown = json.dumps(public_envelope(outcome.invocation_id))
        self.assertIsNone(public_envelope(outcome.invocation_id)["evidence"])
        self.assertNotIn("SECRET_BOOM", shown)
        self.assertNotIn(SECRET, shown)

    def test_wrong_schema_and_bad_digest_are_rejected(self) -> None:
        def bad(ctx, result):
            produced = _readback(ctx, result)
            produced["schema"] = "other.schema"
            return produced

        self._register(bad)
        outcome = _run(self.root / "schema")
        self.assertTrue(outcome.success)
        self.assertIsNone(get_invocation(outcome.invocation_id).evidence)

    def test_nonzero_iserror_and_denial_have_no_positive_evidence(self) -> None:
        self._register()
        failed = _run(self.root / "bad", status="FAILED", exit_code=1, write=True)
        self.assertFalse(failed.success)
        self.assertEqual(failed.call_outcome, "failed")
        self.assertIsNone(get_invocation(failed.invocation_id).evidence)
        self.assertIsNone(receipt_for(failed))

        remote = _module()
        remote["kind"] = "mcp_remote"
        remote["mcp_server"] = "demo"
        remote["mcp_tool"] = "write"
        with patch("runner.cordis_tools.find_module", return_value=remote):
            with patch("runner.mcp_client.call_remote", return_value=RemoteToolError("MCP error: no")):
                errored = run_cordis_tool("call_job", _call(str(self.root / "remote")))
        self.assertEqual(errored.call_outcome, "failed")
        self.assertIsNone(get_invocation(errored.invocation_id).evidence)

        with patch.dict("os.environ", {"AGENTS_APPROVAL_CMD": "false", "AGENTS_APPROVAL_MODE": "ask"}, clear=False):
            with patch("runner.cordis_tools.find_module", return_value=_module()):
                with patch("runner.cordis_tools.execute_job") as execute:
                    denied = run_cordis_tool("call_job", _call(str(self.root / "denied")))
        execute.assert_not_called()
        self.assertEqual(denied.call_outcome, "denied")
        self.assertIsNone(get_invocation(denied.invocation_id).evidence)
        self.assertIsNone(receipt_for(denied))

    def test_timeout_does_not_claim_the_write_is_absent(self) -> None:
        self._register()
        path = self.root / "timed"
        outcome = _run(path, status="TIMEOUT", exit_code=-1, write=True)
        self.assertEqual(outcome.call_outcome, "timed_out")
        self.assertIsNone(get_invocation(outcome.invocation_id).evidence)
        self.assertNotIn("not written", outcome.text.lower())
        self.assertNotIn("did not write", json.dumps(public_envelope(outcome.invocation_id)).lower())

    def test_evidence_for_one_target_is_not_a_generic_grant(self) -> None:
        self._register()
        outcome = _run(self.root / "grant")
        evidence = get_invocation(outcome.invocation_id).evidence
        assert evidence is not None
        self.assertNotEqual(evidence.target, OTHER)
        self.assertNotIn("cleared", public_envelope(outcome.invocation_id))
        self.assertNotIn("claim", public_envelope(outcome.invocation_id)["evidence"])


if __name__ == "__main__":
    unittest.main()
