"""Turn evidence is visible only to the context that owns the scope.

Copied contexts, child tasks, and worker threads do not inherit it. The
registered final check sees the synthetic write for that turn.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from runner.adapter_evidence import (
    adopt_turn_evidence,
    get_current_turn_evidence,
    get_invocation,
    public_envelope,
    turn_evidence_scope,
)
from runner.cordis_tools import run_cordis_tool
from runner.loop import LOOP_TRAILER_MARKER, main as loop_main
from runner.providers import CompletionRequest, CompletionResult
from runner.receipts import clear_final_check, final_check_findings, register_final_check
from tests.test_adapter_evidence import (
    SECRET,
    TOOL,
    _call,
    _decoys,
    _module,
    _readback,
    _transformed,
    clear_result_validators,
    register_result_validator,
)


def _ids(view) -> list[str]:
    return [record.invocation_id for record in view]


class TestTurnEvidence(unittest.TestCase):
    def setUp(self) -> None:
        clear_result_validators()
        clear_final_check()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        register_result_validator(TOOL, "synthetic", "example.write_result", "1", _readback)

    def tearDown(self) -> None:
        clear_final_check()
        clear_result_validators()
        self._tmp.cleanup()

    def _write(self, name: str, fact: str):
        path = self.root / name
        path.write_bytes(_transformed(fact))
        return path

    def _run(self, name: str, fact: str):
        path = self._write(name, fact)
        with patch("runner.cordis_tools.find_module", return_value=_module()):
            with patch(
                "runner.cordis_tools.execute_job",
                return_value={"status": "SUCCESS", "exit_code": 0, "stdout": _decoys(), "stderr": ""},
            ):
                return run_cordis_tool("call_job", _call(str(path), fact))

    def test_scope_orders_this_turn_and_hides_it_outside(self) -> None:
        self.assertEqual(list(inspect.signature(final_check_findings).parameters), ["text", "receipts"])
        outside = self._run("out", "outside")
        self.assertEqual(get_current_turn_evidence(), ())
        self.assertIsNotNone(get_invocation(outside.invocation_id))
        with turn_evidence_scope():
            self.assertEqual(get_current_turn_evidence(), ())
            first = self._run("one", "alpha")
            second = self._run("two", "beta")
            view = get_current_turn_evidence()
            self.assertIsInstance(view, tuple)
            self.assertEqual(_ids(view), [first.invocation_id, second.invocation_id])
            self.assertIsNot(view, get_current_turn_evidence())
            self.assertTrue(view[0].evidence.target.startswith("gen_"))
            self.assertFalse(hasattr(view[0], "arguments"))
            shown = json.dumps({"public": public_envelope(first.invocation_id)})
            self.assertNotIn(view[0].evidence.target, shown)
            self.assertNotIn(view[0].args_digest, shown)
        self.assertEqual(get_current_turn_evidence(), ())
        self.assertEqual(_ids(view), [first.invocation_id, second.invocation_id])

    def test_nested_retry_and_closed_scope_stay_isolated(self) -> None:
        with turn_evidence_scope():
            outer = self._run("outer", "outer")
            with turn_evidence_scope():
                inner = self._run("inner", "inner")
                self.assertEqual(_ids(get_current_turn_evidence()), [inner.invocation_id])
                saved = get_current_turn_evidence()
            self.assertEqual(_ids(get_current_turn_evidence()), [outer.invocation_id])
            self.assertEqual(_ids(saved), [inner.invocation_id])
        with turn_evidence_scope():
            replay = self._run("outer", "outer")
            self.assertEqual(_ids(get_current_turn_evidence()), [replay.invocation_id])
        self.assertNotEqual(outer.invocation_id, replay.invocation_id)
        self.assertEqual(get_current_turn_evidence(), ())

    def test_exception_and_cancel_reset_the_scope(self) -> None:
        with self.assertRaises(RuntimeError):
            with turn_evidence_scope():
                self._run("boom", "boom")
                raise RuntimeError("stop")
        self.assertEqual(get_current_turn_evidence(), ())
        with self.assertRaises(asyncio.CancelledError):
            with turn_evidence_scope():
                self._run("cancel", "cancel")
                raise asyncio.CancelledError()
        self.assertEqual(get_current_turn_evidence(), ())

    def test_copied_contexts_are_denied_until_adopted(self) -> None:
        with turn_evidence_scope():
            parent = self._run("parent", "parent")
            copied = contextvars.copy_context()

            def denied():
                extra = self._run("denied", "denied")
                return extra.invocation_id, _ids(get_current_turn_evidence())

            extra_id, denied_view = copied.run(denied)
            self.assertEqual(denied_view, [])
            self.assertEqual(_ids(get_current_turn_evidence()), [parent.invocation_id])
            self.assertNotIn(extra_id, _ids(get_current_turn_evidence()))

            def opted():
                with adopt_turn_evidence():
                    extra = self._run("opted", "opted")
                    return extra.invocation_id, _ids(get_current_turn_evidence())

            opted_id, opted_view = copied.run(opted)
            self.assertEqual(opted_view, [parent.invocation_id, opted_id])
            self.assertEqual(_ids(get_current_turn_evidence()), [parent.invocation_id, opted_id])
        self.assertEqual(copied.run(get_current_turn_evidence), ())

    def test_async_child_and_worker_threads_do_not_inherit(self) -> None:
        box: dict = {}

        async def parent() -> None:
            with turn_evidence_scope():
                outcome = self._run("async", "async")

                async def child():
                    return _ids(get_current_turn_evidence())

                async def adopted():
                    with adopt_turn_evidence():
                        return _ids(get_current_turn_evidence())

                box["child"] = await asyncio.create_task(child())
                box["adopted"] = await asyncio.create_task(adopted())
                box["parent"] = _ids(get_current_turn_evidence())
                box["id"] = outcome.invocation_id

        asyncio.run(parent())
        self.assertEqual(box["child"], [])
        self.assertEqual(box["adopted"], [box["id"]])
        self.assertEqual(box["parent"], [box["id"]])

        seen: dict = {}
        with turn_evidence_scope():
            outcome = self._run("thread", "thread")
            copied = contextvars.copy_context()

            def plain() -> None:
                seen["plain"] = _ids(get_current_turn_evidence())

            def opted() -> None:
                with adopt_turn_evidence():
                    seen["adopted"] = _ids(get_current_turn_evidence())

            first = threading.Thread(target=plain)
            second = threading.Thread(target=lambda: copied.run(opted))
            first.start()
            second.start()
            first.join()
            second.join()
            seen["parent"] = _ids(get_current_turn_evidence())
            seen["id"] = outcome.invocation_id
        self.assertEqual(seen["plain"], [])
        self.assertEqual(seen["adopted"], [seen["id"]])
        self.assertEqual(seen["parent"], [seen["id"]])

    def test_parallel_turns_do_not_share_evidence(self) -> None:
        gate = threading.Barrier(2)
        found: dict[str, list[str]] = {}

        def once(name: str, fact: str) -> None:
            path = self._write(name, fact)
            with turn_evidence_scope():
                gate.wait()
                outcome = run_cordis_tool("call_job", _call(str(path), fact))
                found[name] = _ids(get_current_turn_evidence())
                found[name + "_own"] = [outcome.invocation_id]

        with patch("runner.cordis_tools.find_module", return_value=_module()):
            with patch(
                "runner.cordis_tools.execute_job",
                return_value={"status": "SUCCESS", "exit_code": 0, "stdout": _decoys(), "stderr": ""},
            ):
                threads = [
                    threading.Thread(target=once, args=("left", "left")),
                    threading.Thread(target=once, args=("right", "right")),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
        self.assertEqual(found["left"], found["left_own"])
        self.assertEqual(found["right"], found["right_own"])
        self.assertNotEqual(found["left"], found["right"])
        self.assertEqual(get_current_turn_evidence(), ())

    def test_skill_success_is_not_turn_evidence(self) -> None:
        with turn_evidence_scope():
            for name in ("skill.list", "skill.catalog", "skill.load"):
                outcome = run_cordis_tool(
                    "call_job",
                    {"function": {"arguments": json.dumps({"name": name, "arguments": {"name": "missing"}})}},
                )
                self.assertEqual(outcome.invocation_id, "")
            self.assertEqual(get_current_turn_evidence(), ())

    def test_registered_final_check_sees_only_this_turn(self) -> None:
        seen: list[list[str]] = []

        def hook(text, receipts):
            self.assertIsInstance(receipts, list)
            seen.append([record.evidence.target for record in get_current_turn_evidence()])
            return None

        register_final_check(hook)
        with turn_evidence_scope():
            first = self._run("hook-a", "alpha")
            final_check_findings("stored alpha", [])
        with turn_evidence_scope():
            second = self._run("hook-b", "beta")
            final_check_findings("stored beta", [])
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], [get_invocation(first.invocation_id).evidence.target])
        self.assertEqual(seen[1], [get_invocation(second.invocation_id).evidence.target])
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual(final_check_findings("stored alpha", []), [])

    def test_loop_final_check_sees_the_synthetic_write(self) -> None:
        path = self._write("loop", SECRET)
        captured: list = []

        def hook(text, receipts):
            captured.append(get_current_turn_evidence())
            self.assertNotIn(path.name, json.dumps(receipts))
            return None

        def provider(req: CompletionRequest, nth: int) -> CompletionResult:
            if any(message.get("role") == "tool" for message in req.messages):
                return CompletionResult(text="Stored.")
            return CompletionResult(
                text="",
                tool_calls=[
                    {
                        "id": "c_write",
                        "type": "function",
                        "function": {
                            "name": "call_job",
                            "arguments": json.dumps(
                                {"name": TOOL, "arguments": {"dest": str(path), "fact": SECRET}}
                            ),
                        },
                    }
                ],
            )

        seen_requests: list[CompletionRequest] = []

        class Wrapped:
            def complete(self, req: CompletionRequest) -> CompletionResult:
                seen_requests.append(req)
                return provider(req, len(seen_requests))

        register_final_check(hook)
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "AGENTS_HOME": tmp,
                "AGENTS_TRACES_DIR": str(Path(tmp) / "traces"),
                "AGENTS_IDENTITY_PATH": str(Path(tmp) / "identity.json"),
                "AGENTS_CONTENT_DIR": str(Path(tmp) / "content"),
                "AGENTS_MEMORY_RULES": "0",
                "PATH": os.environ.get("PATH", ""),
            }
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, env, clear=True):
                with patch("runner.loop.get_provider", return_value=Wrapped()):
                    with patch("runner.cordis_tools.find_module", return_value=_module()):
                        with patch(
                            "runner.cordis_tools.execute_job",
                            return_value={
                                "status": "SUCCESS",
                                "exit_code": 0,
                                "stdout": _decoys(),
                                "stderr": "",
                            },
                        ):
                            with redirect_stdout(out), redirect_stderr(err):
                                rc = loop_main(
                                    [
                                        "--channel",
                                        "local",
                                        "--user",
                                        "evidence",
                                        "--new-session",
                                        "--message",
                                        "save it",
                                        "--complete",
                                        "--provider",
                                        "fake",
                                        "--deliver",
                                        "buffered",
                                    ]
                                )
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(len(captured), 1)
        view = captured[0]
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0].evidence.persisted_sha256, hashlib.sha256(_transformed(SECRET)).hexdigest())
        self.assertNotIn(view[0].evidence.target, out.getvalue())
        self.assertNotIn(view[0].evidence.persisted_sha256, out.getvalue())
        self.assertNotIn(SECRET, out.getvalue().split(LOOP_TRAILER_MARKER)[-1])
        self.assertEqual(get_current_turn_evidence(), ())
