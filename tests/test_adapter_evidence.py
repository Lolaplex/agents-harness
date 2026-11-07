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

from runner import adapter_evidence
from runner.adapter_evidence import (
    ArgumentDomainError,
    clear_result_validators,
    get_invocation,
    open_invocation,
    public_envelope,
    register_result_validator,
)
from runner.cordis_tools import receipt_for, run_cordis_tool
from runner.mcp_client import RemoteToolError
from runner.receipts import args_digest, format_receipt_context

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


def _assign(obj, key, value) -> None:
    obj[key] = value


def _mutation_report(ctx) -> dict:
    nested = ctx.arguments["nested"]
    items = nested["items"]
    report = {
        "fact": ctx.arguments["fact"],
        "flag": nested["flag"],
        "items": list(items),
        "inner": items[1]["b"],
        "arg_type": type(ctx.arguments).__name__,
        "list_type": type(items).__name__,
        "errors": [],
    }
    probes = (
        ("fact", lambda: _assign(ctx.arguments, "fact", "MUTATED")),
        ("flag", lambda: _assign(nested, "flag", True)),
        ("item", lambda: _assign(items, 0, "z")),
        ("inner", lambda: _assign(items[1], "b", 9)),
    )
    for label, fn in probes:
        try:
            fn()
        except TypeError:
            report["errors"].append(label)
        else:
            report["errors"].append(label + ":wrote")
    try:
        items.append("extra")
    except AttributeError:
        report["errors"].append("append")
    else:
        report["errors"].append("append:wrote")
    report["fact_after"] = ctx.arguments["fact"]
    report["items_after"] = list(ctx.arguments["nested"]["items"])
    return report


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
        envelope = public_envelope(outcome.invocation_id)
        self.assertNotIn("args_digest", envelope)
        shown = json.dumps(envelope)
        self.assertNotIn(SECRET, shown)
        self.assertNotIn(evidence.target, shown)
        self.assertNotIn(evidence.persisted_sha256, shown)
        self.assertNotIn(record.args_digest, shown)
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
        path.write_bytes(_transformed(SECRET))

        def execute(mod, extra_argv=None):
            return _job(path, SECRET, write=False)

        outcomes: list = []

        def once() -> None:
            outcomes.append(run_cordis_tool("call_job", _call(str(path))))

        import runner.cordis_tools as cordis_tools
        from runner.executor import execute_job as real_execute_job
        from runner.modules import find_module as real_find_module

        # One patch on this thread. Patching inside the workers races and can
        # leave the stub installed for later tests.
        for _ in range(20):
            outcomes.clear()
            with patch("runner.cordis_tools.find_module", return_value=_module()):
                with patch("runner.cordis_tools.execute_job", side_effect=execute):
                    threads = [threading.Thread(target=once) for _ in range(2)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join()
            self.assertIs(cordis_tools.execute_job, real_execute_job)
            self.assertIs(cordis_tools.find_module, real_find_module)
            ids = [item.invocation_id for item in outcomes]
            self.assertEqual(len(set(ids)), 2)
            records = [get_invocation(item) for item in ids]
            self.assertEqual(records[0].args_digest, records[1].args_digest)
            self.assertIsNot(records[0], records[1])
            self.assertEqual(records[0].evidence.persisted_sha256, records[1].evidence.persisted_sha256)
            for record in records:
                shown = json.dumps(public_envelope(record.invocation_id))
                self.assertNotIn("args_digest", public_envelope(record.invocation_id))
                self.assertNotIn(record.args_digest, shown)

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

    def test_nested_snapshot_ignores_validator_mutation(self) -> None:
        args = {"fact": SECRET, "nested": {"flag": False, "items": ["a", {"b": 1}]}}
        digest_before = args_digest(args)
        seen: dict = {}

        def evil(ctx, _raw):
            seen["report"] = _mutation_report(ctx)
            seen["same"] = ctx.arguments is args
            seen["same_items"] = ctx.arguments["nested"]["items"] is args["nested"]["items"]
            return {
                "operation": "create",
                "target": "gen_nested",
                "persisted_sha256": "ab" * 32,
                "persisted_bytes": 4,
            }

        self._register(evil)
        record = open_invocation(TOOL, args, "succeeded", {"ok": True})
        args["fact"] = "MUTATED"
        args["nested"]["flag"] = True
        args["nested"]["items"].append("later")
        report = seen["report"]
        self.assertEqual(report["errors"], ["fact", "flag", "item", "inner", "append"])
        self.assertEqual(report["fact"], SECRET)
        self.assertEqual(report["fact_after"], SECRET)
        self.assertEqual(report["flag"], False)
        self.assertEqual(report["items"], ["a", {"b": 1}])
        self.assertEqual(report["items_after"], ["a", {"b": 1}])
        self.assertEqual(report["inner"], 1)
        self.assertEqual(report["arg_type"], "mappingproxy")
        self.assertEqual(report["list_type"], "tuple")
        self.assertFalse(seen["same"])
        self.assertFalse(seen["same_items"])
        self.assertEqual(args["fact"], "MUTATED")
        self.assertEqual(record.args_digest, digest_before)
        self.assertNotEqual(record.args_digest, args_digest(args))
        self.assertIsNotNone(record.evidence)
        envelope = public_envelope(record.invocation_id)
        self.assertNotIn("args_digest", envelope)
        shown = json.dumps(envelope)
        self.assertNotIn(digest_before, shown)
        self.assertNotIn(SECRET, shown)
        self.assertNotIn("ab" * 32, shown)
        self.assertNotIn("gen_nested", shown)

    def test_tool_nested_mutation_does_not_enter_the_digest(self) -> None:
        path = self.root / "sealed"
        path.write_bytes(_transformed(SECRET))
        payload = {
            "dest": str(path),
            "fact": SECRET,
            "nested": {"flag": False, "items": ["a", {"b": 1}]},
        }
        digest_before = args_digest(payload)
        seen: dict = {}

        def evil(ctx, raw):
            seen["report"] = _mutation_report(ctx)
            return _readback(ctx, raw)

        self._register(evil)

        def remote(_server, _tool, arguments):
            arguments["fact"] = "MUTATED"
            arguments["nested"]["flag"] = True
            arguments["nested"]["items"][1]["b"] = 9
            arguments["nested"]["items"].append("from-tool")
            return "ok"

        remote_mod = _module()
        remote_mod["kind"] = "mcp_remote"
        remote_mod["mcp_server"] = "demo"
        remote_mod["mcp_tool"] = "write"
        call = {"function": {"arguments": json.dumps({"name": TOOL, "arguments": payload})}}
        with patch("runner.cordis_tools.find_module", return_value=remote_mod):
            with patch("runner.mcp_client.call_remote", side_effect=remote):
                outcome = run_cordis_tool("call_job", call)
        self.assertEqual(outcome.call_outcome, "succeeded")
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.arguments["fact"], SECRET)
        self.assertFalse(outcome.arguments["nested"]["flag"])
        self.assertEqual(outcome.arguments["nested"]["items"], ["a", {"b": 1}])
        self.assertEqual(outcome.arguments["nested"]["items"][1]["b"], 1)
        report = seen["report"]
        self.assertEqual(report["errors"], ["fact", "flag", "item", "inner", "append"])
        self.assertEqual(report["fact"], SECRET)
        self.assertEqual(report["items_after"], ["a", {"b": 1}])
        self.assertFalse(report["flag"])
        record = get_invocation(outcome.invocation_id)
        assert record is not None and record.evidence is not None
        self.assertEqual(record.args_digest, digest_before)
        mutated = {
            "dest": str(path),
            "fact": "MUTATED",
            "nested": {"flag": True, "items": ["a", {"b": 9}, "from-tool"]},
        }
        self.assertNotEqual(record.args_digest, args_digest(mutated))
        legacy = receipt_for(outcome)
        assert legacy is not None
        self.assertEqual(legacy["args_digest"], digest_before)
        self.assertNotIn("evidence", legacy)
        envelope = public_envelope(outcome.invocation_id)
        self.assertNotIn("args_digest", envelope)
        shown = json.dumps(envelope)
        self.assertNotIn(digest_before, shown)
        self.assertNotIn(record.evidence.persisted_sha256, shown)
        self.assertNotIn(record.evidence.target, shown)
        self.assertNotIn(SECRET, shown)
        self.assertNotIn(str(path), shown)

    def _assert_domain_error(self, fn) -> None:
        with self.assertRaises(ArgumentDomainError) as caught:
            fn()
        err = caught.exception
        self.assertEqual(str(err), "unsupported arguments")
        self.assertIsNone(err.__cause__)
        self.assertIsNone(err.__context__)
        self.assertNotIn("SECRET", str(err))
        self.assertNotIn("SECRET_REPR", str(err))

    def test_set_and_bytearray_are_not_shared_or_executed(self) -> None:
        group = {"keep"}
        buf = bytearray(b"orig")
        nested = {"group": group, "buf": buf, "inner": [group, buf]}
        ran = {"validator": False, "tool": False}

        def evil(ctx, _raw):
            ran["validator"] = True
            return {
                "operation": "create",
                "target": "gen_bad",
                "persisted_sha256": "ab" * 32,
                "persisted_bytes": 1,
            }

        self._register(evil)
        before = len(adapter_evidence._records)
        self._assert_domain_error(lambda: open_invocation(TOOL, nested, "succeeded", {"ok": True}))
        self.assertFalse(ran["validator"])
        self.assertEqual(group, {"keep"})
        self.assertEqual(bytes(buf), b"orig")
        self.assertEqual(len(adapter_evidence._records), before)

        def execute(mod, extra_argv=None):
            ran["tool"] = True
            group.add("tool")
            buf.extend(b"T")
            return _job(self.root / "nope", SECRET, write=False)

        call = {
            "function": {
                "arguments": {
                    "name": TOOL,
                    "arguments": {"dest": str(self.root / "nope"), "nested": nested},
                }
            }
        }
        with patch("runner.cordis_tools.find_module", return_value=_module()):
            with patch("runner.cordis_tools.execute_job", side_effect=execute):
                outcome = run_cordis_tool("call_job", call)
        self.assertFalse(ran["tool"])
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.invocation_id, "")
        self.assertEqual(outcome.arguments, {})
        self.assertEqual(str(outcome.text), "error: unsupported arguments")
        self.assertNotIn("SECRET", str(outcome.text))
        self.assertEqual(group, {"keep"})
        self.assertEqual(bytes(buf), b"orig")
        self.assertIsNone(receipt_for(outcome))

    def test_custom_repr_cycle_and_non_json_values_are_rejected(self) -> None:
        class Boom:
            def __hash__(self) -> int:
                return 1

            def __eq__(self, other: object) -> bool:
                return False

            def __repr__(self) -> str:
                raise RuntimeError("SECRET_REPR")

            def __str__(self) -> str:
                raise RuntimeError("SECRET_REPR")

        boom = Boom()
        self._assert_domain_error(lambda: open_invocation(TOOL, {"x": boom}, "succeeded", None))
        self._assert_domain_error(lambda: open_invocation(TOOL, {boom: "v"}, "succeeded", None))
        cycle: list = []
        cycle.append(cycle)
        self._assert_domain_error(lambda: open_invocation(TOOL, {"c": cycle}, "succeeded", None))
        loop = {"name": "loop"}
        loop["self"] = loop
        self._assert_domain_error(lambda: open_invocation(TOOL, loop, "succeeded", None))
        self._assert_domain_error(lambda: open_invocation(TOOL, {"n": float("nan")}, "succeeded", None))
        self._assert_domain_error(lambda: open_invocation(TOOL, {"n": float("inf")}, "succeeded", None))
        self._assert_domain_error(lambda: open_invocation(TOOL, {"n": float("-inf")}, "succeeded", None))
        self._assert_domain_error(lambda: open_invocation(TOOL, {1: "a"}, "succeeded", None))
        deep: object = "x"
        for _ in range(40):
            deep = [deep]
        self._assert_domain_error(lambda: open_invocation(TOOL, deep, "succeeded", None))
        call = {"function": {"arguments": {"name": TOOL, "arguments": {"x": boom}}}}
        with patch("runner.cordis_tools.find_module", return_value=_module()):
            with patch("runner.cordis_tools.execute_job") as execute:
                outcome = run_cordis_tool("call_job", call)
        execute.assert_not_called()
        self.assertEqual(str(outcome.text), "error: unsupported arguments")
        self.assertNotIn("SECRET_REPR", str(outcome.text))
        self.assertEqual(outcome.invocation_id, "")

    def test_tuple_arguments_are_arrays_and_detached(self) -> None:
        child = {"b": 1}
        args = {"items": ("a", child), "also": child}
        seen: dict = {}

        def evil(ctx, _raw):
            seen["kind"] = type(ctx.arguments["items"]).__name__
            seen["same_tuple"] = ctx.arguments["items"] is args["items"]
            seen["same_child"] = ctx.arguments["items"][1] is child
            seen["shared"] = ctx.arguments["items"][1] is ctx.arguments["also"]
            return {
                "operation": "create",
                "target": "gen_tuple",
                "persisted_sha256": "ab" * 32,
                "persisted_bytes": 2,
            }

        self._register(evil)
        record = open_invocation(TOOL, args, "succeeded", {"ok": True})
        self.assertEqual(seen["kind"], "tuple")
        self.assertFalse(seen["same_tuple"])
        self.assertFalse(seen["same_child"])
        self.assertFalse(seen["shared"])
        self.assertEqual(record.args_digest, args_digest(args))
        self.assertEqual(record.args_digest, args_digest({"items": ["a", {"b": 1}], "also": {"b": 1}}))
        child["b"] = 9
        self.assertEqual(get_invocation(record.invocation_id).args_digest, args_digest({"items": ["a", {"b": 1}], "also": {"b": 1}}))
        shown = json.dumps(public_envelope(record.invocation_id))
        self.assertNotIn("args_digest", public_envelope(record.invocation_id))
        self.assertNotIn(record.args_digest, shown)


if __name__ == "__main__":
    unittest.main()
