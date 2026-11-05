"""Receipts carried into the next step, and a host check that flags an unbacked claim."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from runner.loop import LOOP_TRAILER_MARKER, main as loop_main
from runner.providers import CompletionRequest, CompletionResult
from runner.receipts import (
    RECEIPT_SCHEMA,
    clear_final_check,
    extract_receipt_payload,
    final_check_findings,
    make_receipt,
    register_final_check,
)
from runner.retrieve import reset_content_state


MEMORY_RECEIPT = {
    "schema": "agents_memory.write_receipt.v1",
    "id": "user/note",
    "path": "user/note",
    "sha256": "ab" * 32,
    "bytes": 4,
    "written_at": "2026-11-05T18:00:00Z",
    "verified": True,
}


def _saved_without_write(text: str, receipts: list[dict]) -> str | None:
    if "saved" not in text.lower():
        return None
    backed = [
        item
        for item in receipts
        if item.get("success")
        and (
            str(item.get("tool") or "").endswith(".add")
            or str(item.get("tool") or "").endswith(".write")
            or (isinstance(item.get("receipt"), dict) and item["receipt"].get("verified") is True)
        )
    ]
    if backed:
        return None
    return "claimed saved without a successful write receipt"


class TestRemoteErrorReceipt(unittest.TestCase):
    def tearDown(self) -> None:
        clear_final_check()

    def _remote(self, output: str):
        from runner.cordis_tools import receipt_for, run_cordis_tool

        mod = {
            "name": "mcp.demo.write",
            "kind": "mcp_remote",
            "when": "on_request",
            "cadence": "on_request",
            "verb": "mcp://demo/write",
            "rests_on": "fake",
            "expected_exit": 0,
            "mutates": True,
            "mcp_server": "demo",
            "mcp_tool": "write",
        }
        call = {
            "function": {
                "arguments": json.dumps(
                    {"name": "mcp.demo.write", "arguments": {"text": "note"}}
                )
            }
        }
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("AGENTS_APPROVAL_CMD", "AGENTS_APPROVAL_MODE")
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("runner.cordis_tools.find_module", return_value=mod):
                with patch("runner.mcp_client.call_remote", return_value=output):
                    outcome = run_cordis_tool("call_job", call)
        return outcome, receipt_for(outcome)

    def test_is_error_is_not_a_success_receipt(self) -> None:
        from runner.mcp_client import RemoteToolError, _content_text
        from types import SimpleNamespace

        flagged = _content_text(
            SimpleNamespace(
                isError=True,
                content=[
                    SimpleNamespace(
                        text=json.dumps(
                            {
                                "schema": "agents_memory.write_receipt.v1",
                                "verified": True,
                            }
                        )
                    )
                ],
            )
        )
        self.assertIsInstance(flagged, RemoteToolError)
        outcome, receipt = self._remote(flagged)
        self.assertFalse(outcome.success)
        self.assertTrue(str(outcome.text).startswith("MCP error:"))
        self.assertIsNone(receipt)

        plain = _content_text(
            SimpleNamespace(isError=False, content=[SimpleNamespace(text="MCP error: not an error flag")])
        )
        self.assertNotIsInstance(plain, RemoteToolError)

    def test_successful_remote_receipt_does_not_take_success_from_payload(self) -> None:
        body = json.dumps({"schema": "demo.receipt.v1", "verified": False, "success": False})
        outcome, receipt = self._remote(body)
        self.assertTrue(outcome.success)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertTrue(receipt["success"])
        self.assertEqual(receipt["receipt"]["success"], False)
        self.assertFalse(receipt["receipt"]["verified"])


class TestReceiptPayload(unittest.TestCase):
    def test_extracts_schema_object_and_ignores_other_json(self) -> None:
        text = "Saved to user/note\n" + json.dumps(MEMORY_RECEIPT) + '\n{"ok": true}\n'
        payload = extract_receipt_payload(text)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["schema"], "agents_memory.write_receipt.v1")
        self.assertTrue(payload["verified"])
        self.assertIsNone(extract_receipt_payload('{"ok": true}'))

    def test_read_only_success_has_no_receipt(self) -> None:
        self.assertIsNone(make_receipt("mcp.calendar.list", {}, "[]", success=True, mutating=False))
        self.assertIsNone(make_receipt("mcp.memory.add", {"fact": "x"}, "nope", success=False, mutating=True))


class TestReceiptLoop(unittest.TestCase):
    def tearDown(self) -> None:
        clear_final_check()
        reset_content_state()

    def _run(self, provider, extra_env: dict | None = None) -> tuple[int, str, str, list]:
        seen: list[CompletionRequest] = []

        class Wrapped:
            def complete(self, req: CompletionRequest) -> CompletionResult:
                seen.append(req)
                return provider(req, len(seen))

        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.update(
                {
                    "AGENTS_HOME": tmp,
                    "AGENTS_TRACES_DIR": str(Path(tmp) / "traces"),
                    "AGENTS_IDENTITY_PATH": str(Path(tmp) / "identity.json"),
                    "AGENTS_CONTENT_DIR": str(Path(tmp) / "content"),
                    "AGENTS_MEMORY_RULES": "0",
                }
            )
            env.pop("AGENTS_APPROVAL_CMD", None)
            if extra_env:
                env.update(extra_env)
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, env, clear=True):
                with patch("runner.loop.get_provider", return_value=Wrapped()):
                    with redirect_stdout(out), redirect_stderr(err):
                        rc = loop_main(
                            [
                                "--channel",
                                "local",
                                "--user",
                                "receipts",
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
        return rc, out.getvalue(), err.getvalue(), seen

    def test_successful_write_receipt_is_in_the_next_context(self) -> None:
        stdout = "Saved to user/note\n" + json.dumps(MEMORY_RECEIPT)

        def provider(req: CompletionRequest, nth: int) -> CompletionResult:
            if any(m.get("role") == "tool" for m in req.messages):
                blob = "\n".join(str(m.get("content") or "") for m in req.messages)
                self.assertIn(RECEIPT_SCHEMA, blob)
                self.assertIn("mcp.memory.add", blob)
                self.assertIn("agents_memory.write_receipt.v1", blob)
                self.assertIn("verified", blob)
                return CompletionResult(text="Stored.")
            return CompletionResult(
                text="",
                tool_calls=[
                    {
                        "id": "c_add",
                        "type": "function",
                        "function": {
                            "name": "call_job",
                            "arguments": json.dumps(
                                {"name": "mcp.memory.add", "arguments": {"fact": "note", "kind": "fact"}}
                            ),
                        },
                    }
                ],
            )

        with patch(
            "runner.cordis_tools.execute_job",
            return_value={"status": "SUCCESS", "stdout": stdout, "stderr": ""},
        ):
            rc, out, err, seen = self._run(provider)
        self.assertEqual(rc, 0, err)
        self.assertIn("Stored.", out.split(LOOP_TRAILER_MARKER)[0])
        self.assertGreaterEqual(len(seen), 2)
        self.assertNotIn("response_flags", out)

    def test_read_only_call_does_not_carry_a_receipt(self) -> None:
        def provider(req: CompletionRequest, nth: int) -> CompletionResult:
            if any(m.get("role") == "tool" for m in req.messages):
                blob = "\n".join(
                    str(m.get("content") or "")
                    for m in req.messages
                    if m.get("role") != "system"
                )
                self.assertNotIn("<tool_receipts>", blob)
                return CompletionResult(text="listed")
            return CompletionResult(
                text="",
                tool_calls=[
                    {
                        "id": "c_list",
                        "type": "function",
                        "function": {
                            "name": "call_job",
                            "arguments": json.dumps({"name": "mcp.calendar.list", "arguments": {}}),
                        },
                    }
                ],
            )

        with patch(
            "runner.cordis_tools.execute_job",
            return_value={"status": "SUCCESS", "stdout": "[]", "stderr": ""},
        ):
            rc, _out, err, _seen = self._run(provider)
        self.assertEqual(rc, 0, err)

    def test_saved_claim_without_receipt_is_flagged_only_with_a_hook(self) -> None:
        clear_final_check()

        def always_saved(req: CompletionRequest, nth: int) -> CompletionResult:
            return CompletionResult(text="Saved the note.")

        rc, out, err, _seen = self._run(always_saved)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("response_flags", out)
        self.assertEqual(final_check_findings("Saved the note.", []), [])

        register_final_check(_saved_without_write)
        rc, out, err, seen = self._run(always_saved)
        self.assertEqual(rc, 0, err)
        trailer = json.loads(out.split(LOOP_TRAILER_MARKER, 1)[1].strip().splitlines()[-1])
        self.assertIn("claimed saved without a successful write receipt", trailer["response_flags"])
        self.assertIn("response flagged", err)
        self.assertGreaterEqual(len(seen), 2)
        self.assertIn("Harness check", str(seen[-1].messages[-1].get("content")))

    def test_saved_claim_with_write_receipt_is_not_flagged(self) -> None:
        register_final_check(_saved_without_write)
        stdout = "Saved to user/note\n" + json.dumps(MEMORY_RECEIPT)

        def provider(req: CompletionRequest, nth: int) -> CompletionResult:
            if any(m.get("role") == "tool" for m in req.messages):
                return CompletionResult(text="Saved the note.")
            return CompletionResult(
                text="",
                tool_calls=[
                    {
                        "id": "c_add",
                        "type": "function",
                        "function": {
                            "name": "call_job",
                            "arguments": json.dumps(
                                {"name": "mcp.memory.add", "arguments": {"fact": "note"}}
                            ),
                        },
                    }
                ],
            )

        with patch(
            "runner.cordis_tools.execute_job",
            return_value={"status": "SUCCESS", "stdout": stdout, "stderr": ""},
        ):
            rc, out, err, _seen = self._run(provider)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("response_flags", out)
        self.assertNotIn("response flagged", err)


if __name__ == "__main__":
    unittest.main()
