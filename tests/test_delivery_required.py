"""Host delivery gate. Legacy stdout stays covered by the existing loop tests."""

from __future__ import annotations

import io
import json
import os
import signal
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from runner.delivery_gate import (
    STDOUT_CAP,
    DeliveryDecision,
    begin_turn_scope,
    clear_delivery_decision,
    encode_envelope,
    end_turn_scope,
    parse_delivery_envelope,
    register_delivery_decision,
    request_turn_cancel,
    turn_cancelled,
    write_terminal,
)
from runner.loop import LOOP_TRAILER_MARKER, main as loop_main
from runner.providers import CompletionRequest, CompletionResult
from runner.receipts import clear_final_check, register_final_check


def _envelope(status: str = "released", reply: str = "ok", **extra: str) -> bytes:
    raw = encode_envelope(
        status,
        reply,
        extra.get("session", "ses"),
        extra.get("user_id", "u"),
        extra.get("alias", "local:probe"),
    )
    if raw is None:
        raise AssertionError("fixture did not encode")
    return raw


class TestEnvelopeParser(unittest.TestCase):
    def test_roundtrip_exact_reply_and_optional_lf(self) -> None:
        reply = 'café "quote"\nline\t\r{"status":"blocked"}'
        raw = encode_envelope("released", reply, "ses", "u", "local:probe")
        self.assertIsNotNone(raw)
        assert raw is not None
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        parsed = parse_delivery_envelope(raw)
        self.assertEqual(parsed["status"], "released")
        self.assertEqual(parsed["reply"], reply)
        self.assertEqual(parse_delivery_envelope(raw[:-1])["reply"], reply)
        self.assertNotEqual(parsed["status"], "blocked")

    def test_non_released_must_have_empty_reply(self) -> None:
        for status in ("blocked", "policy_failure", "cancelled", "timed_out"):
            raw = encode_envelope(status, "", "s", "u", "a")
            assert raw is not None
            self.assertEqual(parse_delivery_envelope(raw)["status"], status)
            self.assertEqual(parse_delivery_envelope(raw)["reply"], "")

    def test_rejects_hostile_bytes(self) -> None:
        good = _envelope("blocked", "")
        body = good[:-1]
        cases = [
            good + b"\n",
            good + b" ",
            b" " + body,
            b"\xef\xbb\xbf" + body,
            body + b"{}",
            b"[]\n",
            b"null\n",
            b"\xff\n",
            body.replace(b'"blocked"', b'"blocked","extra":"1"'),
            body.replace(b'"reply":""', b'"reply":1'),
            body.replace(b'"status":"blocked"', b'"status":"nope"'),
            body.replace(b'"schema":"agents_harness.delivery.v1"', b'"schema":"other"'),
            body.replace(b'"blocked"', b'"released"').replace(b'"reply":""', b'"reply":"x"', 1)
            if False
            else _released_with_blocked_text(),
            b"{" + b"a" * (STDOUT_CAP + 10) + b"}",
            _duplicate_key(),
            _escaped_duplicate_key(),
            _surrogate_reply(),
            _nul_reply(),
            _deep(),
            _wide(),
        ]
        # The False branch above is unused; the helper is appended explicitly.
        cases.append(_blocked_nonempty())
        for raw in cases:
            with self.assertRaises(ValueError):
                parse_delivery_envelope(raw)

    def test_allows_tab_lf_cr_inside_reply(self) -> None:
        parsed = parse_delivery_envelope(_envelope("released", "a\tb\nc\rd"))
        self.assertEqual(parsed["reply"], "a\tb\nc\rd")

    def test_timed_out_is_a_status_the_loop_does_not_emit(self) -> None:
        parsed = parse_delivery_envelope(_envelope("timed_out", ""))
        self.assertEqual(parsed["status"], "timed_out")
        self.assertEqual(parsed["reply"], "")


def _blocked_nonempty() -> bytes:
    raw = _envelope("released", "nope")
    return raw.replace(b'"released"', b'"blocked"', 1)


def _released_with_blocked_text() -> bytes:
    return _blocked_nonempty()


def _duplicate_key() -> bytes:
    raw = _envelope("blocked", "")[:-1]
    return raw.replace(b'"alias":', b'"alias":"x","alias":', 1) + b"\n"


def _escaped_duplicate_key() -> bytes:
    raw = _envelope("blocked", "")[:-1]
    return raw.replace(b'"alias":', b'"\\u0061lias":"x","alias":', 1) + b"\n"


def _surrogate_reply() -> bytes:
    raw = _envelope("released", "ok")[:-1]
    return raw.replace(b'"ok"', b'"\\ud800"', 1) + b"\n"


def _nul_reply() -> bytes:
    raw = _envelope("released", "ok")[:-1]
    return raw.replace(b'"ok"', b'"\\u0000"', 1) + b"\n"


def _deep() -> bytes:
    node: object = "x"
    for _ in range(9):
        node = {"a": node}
    return json.dumps(node).encode("utf-8")


def _wide() -> bytes:
    payload = {f"k{i}": "v" for i in range(40)}
    return json.dumps(payload).encode("utf-8")


class TestTerminalCommit(unittest.TestCase):
    def tearDown(self) -> None:
        end_turn_scope()

    def test_cancel_before_write_drops_reply_and_later_write_does_not_retract(self) -> None:
        begin_turn_scope()
        request_turn_cancel()
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = write_terminal(
                required=True,
                status="released",
                reply="SECRET",
                session="s",
                user_id="u",
                alias="a",
                record=lambda _text: self.fail("recorded"),
            )
        self.assertEqual(code, 0)
        parsed = parse_delivery_envelope(out.getvalue().encode("utf-8"))
        self.assertEqual(parsed["status"], "cancelled")
        self.assertEqual(parsed["reply"], "")
        self.assertNotIn("SECRET", out.getvalue())
        request_turn_cancel()
        with redirect_stdout(out):
            again = write_terminal(
                required=True,
                status="released",
                reply="SECOND",
                session="s",
                user_id="u",
                alias="a",
                record=lambda _text: None,
            )
        self.assertEqual(again, 0)
        self.assertNotIn("SECOND", out.getvalue())
        self.assertEqual(out.getvalue().count("\n"), 1)


class _Tty:
    encoding = "utf-8"

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> int:
        data = text.encode("utf-8")
        self.buffer.write(data)
        return len(text)

    def flush(self) -> None:
        return None


class TestDeliveryLoop(unittest.TestCase):
    def setUp(self) -> None:
        clear_delivery_decision()
        clear_final_check()

    def tearDown(self) -> None:
        clear_delivery_decision()
        clear_final_check()
        end_turn_scope()

    def _run(self, provider, argv_extra=None, hook=None, check=None) -> tuple[int, str, str, list]:
        seen: list[CompletionRequest] = []

        class Wrapped:
            def complete(self, req: CompletionRequest) -> CompletionResult:
                seen.append(req)
                return provider(req, len(seen))

        if hook is not None:
            register_delivery_decision(hook)
        if check is not None:
            register_final_check(check)
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
            for key in (
                "AGENTS_MODULES_DIR",
                "AGENTS_APPROVAL_CMD",
                "AGENTS_SKILLS_DIR",
                "AGENTS_MCP_CONFIG",
                "AGENTS_WORKSPACE_DIR",
            ):
                env.pop(key, None)
            argv = [
                "--channel",
                "local",
                "--user",
                "probe",
                "--new-session",
                "--message",
                "hello",
                "--complete",
                "--provider",
                "fake",
                "--deliver",
                "buffered",
            ]
            if argv_extra:
                argv.extend(argv_extra)
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, env, clear=True):
                with patch("runner.loop.get_provider", return_value=Wrapped()):
                    with redirect_stdout(out), redirect_stderr(err):
                        rc = loop_main(argv)
        return rc, out.getvalue(), err.getvalue(), seen

    def test_required_without_hook_skips_provider(self) -> None:
        def explode(req, nth):
            raise AssertionError("provider")

        rc, out, err, seen = self._run(explode, ["--delivery-required"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen, [])
        parsed = parse_delivery_envelope(out.encode("utf-8"))
        self.assertEqual(parsed["status"], "policy_failure")
        self.assertEqual(parsed["reply"], "")
        self.assertNotIn(LOOP_TRAILER_MARKER, out)

    def test_allow_replace_block_and_invalid(self) -> None:
        def text_of(_req, _nth):
            return CompletionResult(text="DRAFT")

        def allow(_text, _receipts):
            return DeliveryDecision("allow")

        rc, out, _err, seen = self._run(text_of, ["--delivery-required"], allow)
        self.assertEqual(rc, 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(parse_delivery_envelope(out.encode())["reply"], "DRAFT")
        self.assertIsNone(seen[0].on_delta)

        def replace(text, _receipts):
            self.assertEqual(text, "DRAFT")
            return DeliveryDecision("replace", "APPROVED")

        rc, out, _err, _seen = self._run(text_of, ["--delivery-required"], replace)
        self.assertEqual(parse_delivery_envelope(out.encode())["reply"], "APPROVED")
        self.assertNotIn("DRAFT", out)

        def block(_text, _receipts):
            return DeliveryDecision("block", "DRAFT")

        rc, out, err, _seen = self._run(text_of, ["--delivery-required"], block)
        parsed = parse_delivery_envelope(out.encode())
        self.assertEqual(parsed["status"], "blocked")
        self.assertEqual(parsed["reply"], "")
        self.assertNotIn("DRAFT", out)
        self.assertNotIn("DRAFT", err)

        def bad(_text, _receipts):
            return {"action": "allow", "text": "DRAFT"}

        rc, out, err, _seen = self._run(text_of, ["--delivery-required"], bad)
        parsed = parse_delivery_envelope(out.encode())
        self.assertEqual(parsed["status"], "policy_failure")
        self.assertNotIn("DRAFT", out + err)

        def boom(_text, _receipts):
            raise RuntimeError("SECRET-POLICY")

        rc, out, err, _seen = self._run(text_of, ["--delivery-required"], boom)
        parsed = parse_delivery_envelope(out.encode())
        self.assertEqual(parsed["status"], "policy_failure")
        self.assertNotIn("SECRET-POLICY", out + err)

    def test_reply_json_is_not_status(self) -> None:
        payload = '{"status":"blocked","reply":"pwn"}'

        def text_of(_req, _nth):
            return CompletionResult(text=payload)

        def allow(_text, _receipts):
            return DeliveryDecision("allow")

        _rc, out, _err, _seen = self._run(text_of, ["--delivery-required"], allow)
        parsed = parse_delivery_envelope(out.encode())
        self.assertEqual(parsed["status"], "released")
        self.assertEqual(parsed["reply"], payload)

    def test_local_hook_is_plaintext_even_on_a_tty(self) -> None:
        tty = _Tty()
        err = io.StringIO()

        def allow(_text, _receipts):
            return DeliveryDecision("replace", "APPROVED")

        register_delivery_decision(allow)

        class Wrapped:
            def complete(self, req: CompletionRequest) -> CompletionResult:
                self.delta = req.on_delta
                return CompletionResult(text="DRAFT")

        wrapped = Wrapped()
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["AGENTS_HOME"] = tmp
            env["AGENTS_MEMORY_RULES"] = "0"
            env.pop("AGENTS_MODULES_DIR", None)
            with patch.dict(os.environ, env, clear=True):
                with patch("runner.loop.get_provider", return_value=wrapped):
                    with patch("sys.stdout", tty), redirect_stderr(err):
                        rc = loop_main(
                            [
                                "--channel",
                                "local",
                                "--user",
                                "probe",
                                "--new-session",
                                "--message",
                                "hello",
                                "--complete",
                                "--provider",
                                "fake",
                            ]
                        )
        self.assertEqual(rc, 0)
        self.assertIsNone(wrapped.delta)
        self.assertEqual(tty.buffer.getvalue(), b"APPROVED")
        self.assertNotIn(b"DRAFT", tty.buffer.getvalue())
        self.assertNotIn(b"{", tty.buffer.getvalue())
        self.assertNotIn("DRAFT", err.getvalue())

    def test_no_hook_tty_stays_legacy_stream(self) -> None:
        tty = _Tty()

        class Wrapped:
            def complete(self, req: CompletionRequest) -> CompletionResult:
                if req.on_delta:
                    req.on_delta("TOKEN")
                return CompletionResult(text="TOKEN")

        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["AGENTS_HOME"] = tmp
            env["AGENTS_MEMORY_RULES"] = "0"
            with patch.dict(os.environ, env, clear=True):
                with patch("runner.loop.get_provider", return_value=Wrapped()):
                    with patch("sys.stdout", tty):
                        rc = loop_main(
                            [
                                "--channel",
                                "local",
                                "--user",
                                "probe",
                                "--new-session",
                                "--message",
                                "hello",
                                "--complete",
                                "--provider",
                                "fake",
                            ]
                        )
        self.assertEqual(rc, 0)
        raw = tty.buffer.getvalue()
        self.assertIn(b"TOKEN", raw)
        self.assertNotIn(b"agents_harness.delivery.v1", raw)

    def test_local_block_failure_and_empty_write_no_fallback(self) -> None:
        def text_of(_req, _nth):
            return CompletionResult(text="DRAFT")

        def block(_text, _receipts):
            return DeliveryDecision("block")

        rc, out, err, _seen = self._run(text_of, hook=block)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertNotIn("DRAFT", err)
        self.assertNotIn(LOOP_TRAILER_MARKER, out)

        def empty(_text, _receipts):
            return DeliveryDecision("allow")

        def blank(_req, _nth):
            return CompletionResult(text="")

        rc, out, err, _seen = self._run(blank, hook=empty)
        self.assertEqual(out, "")
        self.assertNotIn("delivery withheld", err)

        def crash(_text, _receipts):
            raise RuntimeError("SECRET-POLICY")

        rc, out, err, _seen = self._run(text_of, hook=crash)
        self.assertEqual(out, "")
        self.assertNotIn("SECRET-POLICY", out + err)
        self.assertNotIn("DRAFT", out)

    def test_check_exception_skips_policy_and_revision(self) -> None:
        calls = {"policy": 0, "check": 0}

        def text_of(_req, _nth):
            return CompletionResult(text="DRAFT")

        def check(_text, _receipts):
            calls["check"] += 1
            raise RuntimeError("SECRET-CHECK")

        def policy(_text, _receipts):
            calls["policy"] += 1
            return DeliveryDecision("allow")

        rc, out, err, seen = self._run(text_of, ["--delivery-required"], policy, check)
        self.assertEqual(rc, 0)
        self.assertEqual(calls["check"], 1)
        self.assertEqual(calls["policy"], 0)
        self.assertEqual(len(seen), 1)
        parsed = parse_delivery_envelope(out.encode())
        self.assertEqual(parsed["status"], "policy_failure")
        self.assertNotIn("SECRET-CHECK", out + err)
        self.assertNotIn("DRAFT", out + err)

    def test_one_revision_then_one_policy(self) -> None:
        checks: list[str] = []

        def text_of(_req, nth):
            if nth == 1:
                return CompletionResult(text="DRAFT-SECRET")
            return CompletionResult(text="REVISED-OK")

        def check(text, _receipts):
            checks.append(text)
            if text == "DRAFT-SECRET":
                return "revise"
            raise AssertionError("second check")

        def policy(text, _receipts):
            self.assertEqual(text, "REVISED-OK")
            return DeliveryDecision("allow")

        rc, out, err, seen = self._run(text_of, ["--delivery-required"], policy, check)
        self.assertEqual(rc, 0, err)
        self.assertEqual(checks, ["DRAFT-SECRET"])
        self.assertEqual(len(seen), 2)
        self.assertIsNone(seen[1].tools)
        parsed = parse_delivery_envelope(out.encode())
        self.assertEqual(parsed["reply"], "REVISED-OK")
        self.assertNotIn("DRAFT-SECRET", out)
        self.assertNotIn("response_flags", out)

    def test_provider_error_calls_policy_once_without_the_exception(self) -> None:
        seen_text: list[str] = []

        def text_of(_req, _nth):
            raise RuntimeError("SECRET-PROV")

        def policy(text, _receipts):
            seen_text.append(text)
            return DeliveryDecision("replace", "safe-note")

        rc, out, err, seen = self._run(text_of, ["--delivery-required"], policy)
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen_text, [""])
        self.assertEqual(len(seen), 1)
        parsed = parse_delivery_envelope(out.encode())
        self.assertEqual(parsed["status"], "released")
        self.assertEqual(parsed["reply"], "safe-note")
        self.assertNotIn("SECRET-PROV", out + err)
        self.assertNotIn("Turn failed", out)

    def test_revision_error_is_empty_candidate(self) -> None:
        seen_text: list[str] = []

        def text_of(_req, nth):
            if nth == 1:
                return CompletionResult(text="DRAFT")
            raise RuntimeError("SECRET-REV")

        def check(_text, _receipts):
            return "revise"

        def policy(text, _receipts):
            seen_text.append(text)
            return DeliveryDecision("replace", "after-rev")

        rc, out, err, seen = self._run(text_of, ["--delivery-required"], policy, check)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen_text, [""])
        self.assertEqual(parse_delivery_envelope(out.encode())["reply"], "after-rev")
        self.assertNotIn("SECRET-REV", out + err)

    def test_cancel_before_and_during_policy(self) -> None:
        def draft(_req, _nth):
            request_turn_cancel()
            return CompletionResult(text="SECRET")

        def policy(_text, _receipts):
            raise AssertionError("policy")

        rc, out, err, _seen = self._run(draft, ["--delivery-required"], policy)
        self.assertEqual(rc, 0, err)
        parsed = parse_delivery_envelope(out.encode())
        self.assertEqual(parsed["status"], "cancelled")
        self.assertNotIn("SECRET", out + err)
        self.assertFalse(turn_cancelled())

        def draft2(_req, _nth):
            return CompletionResult(text="SECRET")

        def policy2(_text, _receipts):
            request_turn_cancel()
            return DeliveryDecision("replace", "SECRET")

        rc, out, err, _seen = self._run(draft2, ["--delivery-required"], policy2)
        parsed = parse_delivery_envelope(out.encode())
        self.assertEqual(parsed["status"], "cancelled")
        self.assertEqual(parsed["reply"], "")
        self.assertNotIn("SECRET", out + err)

    def test_sigterm_before_policy_is_cancel(self) -> None:
        def draft(_req, _nth):
            os.kill(os.getpid(), signal.SIGTERM)
            return CompletionResult(text="SECRET")

        def policy(_text, _receipts):
            raise AssertionError("policy")

        rc, out, err, _seen = self._run(draft, ["--delivery-required"], policy)
        self.assertEqual(rc, 0, err)
        self.assertEqual(parse_delivery_envelope(out.encode())["status"], "cancelled")
        self.assertNotIn("SECRET", out + err)

    def test_oversize_writes_nothing(self) -> None:
        def draft(_req, _nth):
            return CompletionResult(text="x" * 300000)

        def allow(_text, _receipts):
            return DeliveryDecision("allow")

        rc, out, err, _seen = self._run(draft, ["--delivery-required"], allow)
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertNotIn("x" * 100, out)

    def test_assistant_record_is_only_released_text(self) -> None:
        recorded: list[tuple[str, str]] = []

        def spy(session, role, content, channel, user, user_id="", project=""):
            recorded.append((role, content))

        def draft(_req, _nth):
            return CompletionResult(text="DRAFT-SECRET")

        def block(_text, _receipts):
            return DeliveryDecision("block")

        with patch("runner.loop._record", spy):
            rc, out, err, _seen = self._run(draft, ["--delivery-required"], block)
        self.assertEqual(rc, 0, err)
        roles = [role for role, _text in recorded]
        self.assertIn("user", roles)
        self.assertNotIn("assistant", roles)
        self.assertNotIn("DRAFT-SECRET", out + err)

        recorded.clear()

        def replace(_text, _receipts):
            return DeliveryDecision("replace", "APPROVED")

        with patch("runner.loop._record", spy):
            _rc, out, _err, _seen = self._run(draft, ["--delivery-required"], replace)
        assistant = [text for role, text in recorded if role == "assistant"]
        self.assertEqual(assistant, ["APPROVED"])
        self.assertEqual(parse_delivery_envelope(out.encode())["reply"], "APPROVED")

        recorded.clear()

        def allow_empty(_text, _receipts):
            return DeliveryDecision("replace", "")

        with patch("runner.loop._record", spy):
            _rc, out, _err, _seen = self._run(draft, ["--delivery-required"], allow_empty)
        self.assertNotIn("assistant", [role for role, _text in recorded])
        self.assertEqual(parse_delivery_envelope(out.encode())["reply"], "")

    def test_block_does_not_rerun_tools(self) -> None:
        calls = {"jobs": 0}

        def provider(req: CompletionRequest, nth: int) -> CompletionResult:
            if any(msg.get("role") == "tool" for msg in req.messages):
                return CompletionResult(text="DRAFT")
            return CompletionResult(
                text="",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "call_job",
                            "arguments": json.dumps({"name": "mcp.memory.search", "arguments": {"q": "x"}}),
                        },
                    }
                ],
            )

        def block(_text, _receipts):
            return DeliveryDecision("block")

        def execute_job(_mod, **_kwargs):
            calls["jobs"] += 1
            return {"status": "SUCCESS", "stdout": "[]", "stderr": ""}

        with patch("runner.cordis_tools.execute_job", side_effect=execute_job):
            rc, out, err, seen = self._run(provider, ["--delivery-required"], block)
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls["jobs"], 1)
        self.assertEqual(len(seen), 2)
        self.assertEqual(parse_delivery_envelope(out.encode())["status"], "blocked")
        self.assertNotIn("DRAFT", out)


if __name__ == "__main__":
    unittest.main()
