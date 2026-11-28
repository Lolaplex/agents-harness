"""Wave 10A: adaptive turn router and compact capability index."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from runner.catalog import (
    COMPACT_SUMMARY_MAX_CHARS,
    compact_catalog_rows,
    render_compact_catalog_summary,
)
from runner.cordis_tools import run_cordis_tool
from runner.delivery import SIDEBAND_PREFIX
from runner.loop import build_payload, main as loop_main
from runner.providers import CompletionResult
from runner.router import classify_turn


def _sideband(text: str) -> list[dict]:
    return [
        json.loads(line[len(SIDEBAND_PREFIX) :])
        for line in text.splitlines()
        if line.startswith(SIDEBAND_PREFIX)
    ]


def _synthetic_catalog(count: int = 213, domains: int = 24) -> list[dict]:
    rows = []
    for i in range(count):
        server = f"agents-server{i % domains}"
        rows.append(
            {
                "name": f"mcp.{server}.tool_{i}",
                "kind": "mcp_remote",
                "when": "on_request",
                "verb": f"mcp://{server}/tool_{i}",
                "mcp_server": server,
                "mcp_tool": f"tool_{i}",
                "rests_on": f"Long schema-facing description for tool {i}. " + ("x" * 220),
                "parameters": {
                    "type": "object",
                    "properties": {"input": {"type": "string"}},
                    "required": ["input"],
                },
            }
        )
    return rows


class TestClassifyTurn(unittest.TestCase):
    def test_greetings_thanks_ack_and_smalltalk_en_de_are_fast(self):
        prompts = [
            "Hi",
            "hey!",
            "hello",
            "yo",
            "hallo",
            "moin",
            "servus",
            "guten Morgen!",
            "danke",
            "vielen Dank",
            "thanks",
            "ok",
            "cool",
            "nice",
            "wie gehts?",
            "how are you?",
            "wer bist du?",
            "who are you?",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(classify_turn(prompt), "fast")

    def test_short_self_contained_conceptual_questions_are_fast(self):
        prompts = [
            "What is the capital of France?",
            "Was ist Photosynthese?",
            "what is recursion?",
            "explain recursion in one sentence",
            "define bionic reading",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(classify_turn(prompt), "fast")

    def test_tool_action_file_code_url_and_mention_triggers_fall_back_to_agent(self):
        prompts = [
            "read README.md",
            "write a file",
            "run the tests",
            "fix this bug",
            "search the repo",
            "zeig mir die datei",
            "what is a file?",
            "hi https://example.com",
            "hi @felix",
            "look at ./src",
            "what is src/main.py?",
            "what is .py?",
            "hi ```python\nprint(1)\n```",
            "x" * 141,
            "one\ntwo\nthree",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(classify_turn(prompt), "agent")

    def test_attachments_and_recent_tool_history_are_not_fast_in_auto(self):
        self.assertEqual(classify_turn("hi", has_attachments=True), "agent")
        self.assertEqual(classify_turn("hi", history_has_tools=True), "agent")

    def test_explicit_modes_win(self):
        self.assertEqual(classify_turn("read README.md", mode="fast"), "fast")
        self.assertEqual(classify_turn("hi", mode="agent"), "agent")
        self.assertEqual(classify_turn("hi", mode="deep"), "deep")
        self.assertEqual(classify_turn("hi", mode="unexpected"), "agent")

    def test_deep_intent_and_long_code_or_file_prompts(self):
        prompts = [
            "refactor the router",
            "implement the feature",
            "architect the migration",
            "audit the codebase",
            "plan the multi-step rollout",
            ("context " * 90) + " src/main.py " + ("details " * 30),
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt[:40]):
                self.assertEqual(classify_turn(prompt), "deep")

    def test_plain_uncertain_or_long_non_code_prompt_is_agent(self):
        self.assertEqual(classify_turn("tell me more about the prior approach"), "agent")
        self.assertEqual(classify_turn("x" * 501), "agent")


class TestFastTierPrompt(unittest.TestCase):
    def test_fast_payload_skips_catalog_assembly_and_has_escalation_pointer(self):
        with patch("runner.loop.list_modules", side_effect=AssertionError("catalog assembled")), patch(
            "runner.loop.skills_prompt_block", side_effect=AssertionError("skills assembled")
        ), patch("runner.loop.pinned_verbs_prompt_block", side_effect=AssertionError("pinned assembled")), patch(
            "runner.loop.project_context_block", side_effect=AssertionError("project context assembled")
        ):
            messages = build_payload(
                "ses_fast",
                "hi",
                user_id="u",
                include_clock=False,
                turn_tier="fast",
            )
        system = messages[0]["content"]
        self.assertIn("<!-- none loaded this request -->", system)
        self.assertNotIn("<skill name=", system)
        self.assertIn("Tools are available via list_catalog/call_job if genuinely needed", system)
        self.assertIn("reply directly in 1-2 sentences without calling tools", system)
        self.assertIn("load_schema", system)

    def test_agent_payload_compact_catalog_and_full_escape_hatch(self):
        rows = _synthetic_catalog(count=12, domains=3)
        patches = [
            patch("runner.loop.list_modules", return_value=rows),
            patch("runner.loop.memory_rules_prompt_block", return_value=""),
            patch("runner.loop.skills_prompt_block", return_value=""),
            patch("runner.loop.pinned_verbs_prompt_block", return_value=""),
            patch("runner.loop.recall_prompt_line", return_value=""),
            patch("runner.loop.project_context_block", return_value=""),
        ]
        for active in patches:
            active.start()
        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("HARNESS_CATALOG_FULL", None)
                compact = build_payload(
                    "ses_compact",
                    "use a tool",
                    user_id="u",
                    include_clock=False,
                    turn_tier="agent",
                )[0]["content"]
            with patch.dict(os.environ, {"HARNESS_CATALOG_FULL": "1"}, clear=False):
                full = build_payload(
                    "ses_full",
                    "use a tool",
                    user_id="u",
                    include_clock=False,
                    turn_tier="agent",
                )[0]["content"]
        finally:
            for active in patches:
                active.stop()
        self.assertIn('<skill name="mcp.server0">', compact)
        self.assertNotIn('<skill name="mcp.agents-server0.tool_0">', compact)
        self.assertIn("list_catalog", compact)
        self.assertIn('<skill name="mcp.agents-server0.tool_0">', full)
        self.assertGreater(len(full), len(compact))

    def test_compact_prompt_is_under_quarter_of_raw_213_row_dump(self):
        rows = _synthetic_catalog()
        patches = [
            patch("runner.loop.list_modules", return_value=rows),
            patch("runner.loop.memory_rules_prompt_block", return_value=""),
            patch("runner.loop.skills_prompt_block", return_value=""),
            patch("runner.loop.pinned_verbs_prompt_block", return_value=""),
            patch("runner.loop.recall_prompt_line", return_value=""),
            patch("runner.loop.project_context_block", return_value=""),
        ]
        for active in patches:
            active.start()
        try:
            with patch.dict(os.environ, {"HARNESS_CATALOG_FULL": "1"}, clear=False):
                full_system = build_payload(
                    "ses_raw",
                    "work",
                    user_id="u",
                    include_clock=False,
                    turn_tier="deep",
                )[0]["content"]
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("HARNESS_CATALOG_FULL", None)
                compact_system = build_payload(
                    "ses_compact",
                    "work",
                    user_id="u",
                    include_clock=False,
                    turn_tier="deep",
                )[0]["content"]
        finally:
            for active in patches:
                active.stop()

        summary = render_compact_catalog_summary(rows)
        compact_rows = compact_catalog_rows(rows)
        compact_row_chars = sum(len(domain) + len(text) + 2 for domain, text in compact_rows)
        self.assertGreater(len(full_system), 50000)
        self.assertLess(len(compact_system), len(full_system) * 0.25)
        self.assertLessEqual(len(summary), COMPACT_SUMMARY_MAX_CHARS)
        self.assertLessEqual(compact_row_chars, COMPACT_SUMMARY_MAX_CHARS)
        self.assertIn("summary only", summary)
        self.assertIn("list_catalog", compact_system)
        self.assertNotIn("Long schema-facing description for tool 212", compact_system)


class TestProgressiveDisclosure(unittest.TestCase):
    def test_list_catalog_query_and_load_schema_keep_full_tool_power(self):
        rows = [
            {
                "name": "mcp.memory.search",
                "kind": "mcp",
                "when": "on_request",
                "verb": "python -m agents_memory search",
                "rests_on": "memory search",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
            {
                "name": "mcp.agents-browser.browser_open",
                "kind": "mcp_remote",
                "when": "on_request",
                "verb": "mcp://agents-browser/browser_open",
                "rests_on": "open browser",
                "mcp_server": "agents-browser",
                "mcp_tool": "browser_open",
                "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
            },
        ]
        with patch("runner.cordis_tools.list_modules", return_value=rows):
            all_out = str(run_cordis_tool("list_catalog", {"function": {"arguments": "{}"}}).text)
            query_out = str(
                run_cordis_tool(
                    "list_catalog",
                    {"function": {"arguments": json.dumps({"query": "mcp.browser"})}},
                ).text
            )
        self.assertIn("mcp.memory.search", all_out)
        self.assertIn("mcp.agents-browser.browser_open", all_out)
        self.assertIn("mcp.agents-browser.browser_open", query_out)
        self.assertNotIn("mcp.memory.search", query_out)

        with patch("runner.cordis_tools.find_module", return_value=rows[1]):
            schema_out = str(
                run_cordis_tool(
                    "load_schema",
                    {"function": {"arguments": json.dumps({"name": "mcp.agents-browser.browser_open"})}},
                ).text
            )
        schema = json.loads(schema_out)
        self.assertEqual(schema["name"], "mcp.agents-browser.browser_open")
        self.assertIn("url", schema["parameters"]["properties"])


class TestLoopTierWiring(unittest.TestCase):
    def _env(self, root: Path) -> dict[str, str]:
        return {
            "AGENTS_HOST_DIR": str(root / "host"),
            "AGENTS_IDENTITY_PATH": str(root / "identity.json"),
            "AGENTS_TRACES_DIR": str(root / "traces"),
            "AGENTS_NO_UPDATE_CHECK": "1",
            "AGENTS_PROJECT_CONTEXT": "0",
            "AGENTS_MEMORY_RULES": "0",
            "AGENTS_CORDIS_EXPOSE": "0",
            "AGENTS_WORKSPACE": "",
        }

    def test_fast_loop_sideband_reasoning_tools_and_trace_metadata(self):
        seen = []

        class CaptureProvider:
            def complete(self, req):
                seen.append(req)
                return CompletionResult(
                    text="hi",
                    usage={"tokens_in": 9, "tokens_out": 2, "est_cost_usd": 0, "model": "m-test"},
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, self._env(root), clear=False), patch(
                "runner.loop.get_provider", return_value=CaptureProvider()
            ), patch("runner.loop.list_modules", side_effect=AssertionError("catalog assembled")), redirect_stdout(out), redirect_stderr(err):
                rc = loop_main(
                    [
                        "--channel",
                        "telegram",
                        "--user",
                        "w10",
                        "--new-session",
                        "--message",
                        "hi",
                        "--complete",
                        "--provider",
                        "echo",
                        "--deliver",
                        "buffered",
                    ]
                )
            self.assertEqual(rc, 0, err.getvalue())
            self.assertTrue(seen)
            req = seen[0]
            self.assertEqual(req.extra.get("reasoning"), {"enabled": False})
            self.assertEqual(
                {tool["function"]["name"] for tool in (req.tools or [])},
                {"list_catalog", "load_schema", "call_job", "clarify"},
            )
            system = req.messages[0]["content"]
            self.assertNotIn("<skill name=", system)
            self.assertIn("reply directly in 1-2 sentences", system)

            events = _sideband(err.getvalue())
            tier_events = [event for event in events if event.get("type") == "tier"]
            self.assertEqual(tier_events, [{"type": "tier", "tier": "fast", "mode": "auto"}])

            trailer = out.getvalue().split("---agents-loop-trailer---\n")[-1]
            session = json.loads(trailer)["session"]
            with patch.dict(os.environ, self._env(root), clear=False):
                from agents_traces.store import TraceStore

                llm_events = [
                    event
                    for event in TraceStore().get_events_for_session(session)
                    if event.type == "llm_call"
                ]
            self.assertTrue(llm_events)
            self.assertEqual(llm_events[0].metadata, {"turn_tier": "fast"})

    def test_explicit_reasoning_is_not_overridden_on_fast_tier(self):
        seen = []

        class CaptureProvider:
            def complete(self, req):
                seen.append(req)
                return CompletionResult(text="hi")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, self._env(root), clear=False), patch(
                "runner.loop.get_provider", return_value=CaptureProvider()
            ), redirect_stdout(out), redirect_stderr(err):
                rc = loop_main(
                    [
                        "--channel",
                        "telegram",
                        "--user",
                        "w10",
                        "--new-session",
                        "--message",
                        "hi",
                        "--complete",
                        "--provider",
                        "echo",
                        "--reasoning",
                        "low",
                        "--deliver",
                        "buffered",
                    ]
                )
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(seen[0].extra.get("reasoning"), {"enabled": True, "effort": "low"})


if __name__ == "__main__":
    unittest.main()
