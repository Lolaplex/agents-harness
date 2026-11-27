import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner import approval_allowlist
from runner.cordis_tools import CordisOutcome
from runner.delivery import SIDEBAND_PREFIX, bind_delivery
from runner.loop import _complete_once, _run_tool_calls
from runner.providers import CompletionRequest, CompletionResult, consume_sse, normalize_usage


def _parse_sideband(err_text: str) -> list[dict]:
    events = []
    for line in err_text.splitlines():
        if line.startswith(SIDEBAND_PREFIX):
            events.append(json.loads(line[len(SIDEBAND_PREFIX) :]))
    return events


class TestU13SidebandAndUsage(unittest.TestCase):
    def test_on_thought_emits_structured_sideband(self):
        out, err = io.StringIO(), io.StringIO()
        _, _, _, on_thought, _ = bind_delivery("http", stdout=out, stderr=err, stdout_tty=False)
        assert on_thought is not None
        on_thought("step 1")
        on_thought(" -> step 2")
        events = _parse_sideband(err.getvalue())
        self.assertEqual(
            events,
            [
                {"type": "thought", "text": "step 1"},
                {"type": "thought", "text": " -> step 2"},
            ],
        )

    def test_consume_sse_captures_empty_choices_usage_and_normalize_usage(self):
        lines = [
            'data: {"choices":[{"delta":{"reasoning":"thinking..."}}]}',
            'data: {"choices":[{"delta":{"content":"done"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":18,"cost":0.0012}}',
            "data: [DONE]",
        ]
        thoughts: list[str] = []
        text, tools, meta = consume_sse(lines, on_thought=thoughts.append)
        self.assertEqual(text, "done")
        self.assertIsNone(tools)
        self.assertEqual(thoughts, ["thinking..."])
        self.assertEqual(meta["usage"]["prompt_tokens"], 42)

        norm_openai = normalize_usage(meta["usage"], model="gpt-test")
        self.assertEqual(
            norm_openai,
            {"tokens_in": 42, "tokens_out": 18, "est_cost_usd": 0.0012, "model": "gpt-test"},
        )
        norm_anthropic = normalize_usage({"input_tokens": 100, "output_tokens": 25}, model="claude-x")
        self.assertEqual(
            norm_anthropic,
            {"tokens_in": 100, "tokens_out": 25, "est_cost_usd": None, "model": "claude-x"},
        )
        self.assertIsNone(normalize_usage({}))

    def test_run_tool_calls_emits_tool_start_and_end_sideband(self):
        status_lines: list[str] = []
        err = io.StringIO()
        fake_outcome = CordisOutcome(
            text="file list ok",
            success=True,
            tool="mcp.terminal.list",
            mutating=False,
            arguments={"path": "."},
        )
        with (
            patch("sys.stderr", err),
            patch("runner.loop.is_cordis_tool", return_value=True),
            patch("runner.loop.run_cordis_tool", return_value=fake_outcome),
        ):
            msgs = _run_tool_calls(
                [{"id": "call_99", "function": {"name": "mcp_terminal_list", "arguments": '{"path":"."}'}}],
                session="",
                on_status=status_lines.append,
            )
        self.assertEqual(len(msgs), 1)
        self.assertTrue(any("running" in s for s in status_lines))
        events = _parse_sideband(err.getvalue())
        types = [e["type"] for e in events]
        self.assertEqual(types, ["tool_start", "tool_end"])
        self.assertEqual(events[0]["id"], "call_99")
        self.assertEqual(events[0]["tool"], "mcp_terminal_list")
        self.assertTrue(events[1]["ok"])
        self.assertIsInstance(events[1]["duration_ms"], (int, float))
        self.assertIn("file list ok", events[1]["result_preview"])

    def test_complete_once_emits_usage_sideband_and_records_llm_trace(self):
        class _FakeProv:
            def complete(self, req: CompletionRequest) -> CompletionResult:
                return CompletionResult(
                    text="ok",
                    raw={"usage": {"prompt_tokens": 11, "completion_tokens": 7, "cost": 0.0005}},
                )

        with tempfile.TemporaryDirectory() as tmp:
            host_dir = Path(tmp) / "host"
            host_dir.mkdir(parents=True, exist_ok=True)
            traces_dir = Path(tmp) / "traces"
            err = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "AGENTS_HOST_DIR": str(host_dir),
                        "AGENTS_TRACES_DIR": str(traces_dir),
                    },
                ),
                patch("sys.stderr", err),
            ):
                res = _complete_once(
                    _FakeProv(),
                    CompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m-test"),
                    session="ses_u13",
                )
                from agents_traces.store import TraceStore

                stored = TraceStore().get_events_for_session("ses_u13")
            self.assertEqual(res.text, "ok")
            events = _parse_sideband(err.getvalue())
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["type"], "usage")
            self.assertEqual(events[0]["tokens_in"], 11)
            self.assertEqual(events[0]["tokens_out"], 7)
            self.assertEqual(events[0]["model"], "m-test")

            llm_events = [e for e in stored if e.type == "llm_call"]
            self.assertEqual(len(llm_events), 1)
            self.assertEqual(llm_events[0].tokens_in, 11)
            self.assertEqual(llm_events[0].tokens_out, 7)
            self.assertEqual(llm_events[0].model, "m-test")


class TestU13AllowlistRemove(unittest.TestCase):
    def test_remove_pattern_removes_existing_and_returns_false_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            allow_path = Path(tmp) / "allowlist.json"
            with patch.dict(os.environ, {"AGENTS_APPROVAL_ALLOWLIST": str(allow_path)}):
                approval_allowlist.remember("mcp_terminal_run", {"command": "pytest"}, pattern="mcp_terminal_run pytest*")
                approval_allowlist.remember("mcp_terminal_run", {"command": "ls"}, pattern="mcp_terminal_run ls*")
                self.assertEqual(
                    approval_allowlist.load_patterns(),
                    ["mcp_terminal_run pytest*", "mcp_terminal_run ls*"],
                )
                self.assertTrue(approval_allowlist.remove_pattern("mcp_terminal_run pytest*"))
                self.assertEqual(approval_allowlist.load_patterns(), ["mcp_terminal_run ls*"])
                self.assertFalse(approval_allowlist.remove_pattern("nonexistent"))


if __name__ == "__main__":
    unittest.main()
