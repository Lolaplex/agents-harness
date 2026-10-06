import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from runner.loop import LOOP_TRAILER_MARKER, _parse_text_tool_calls, build_payload, main as loop_main
from runner.modules import list_modules, openai_tools
from runner.providers import CompletionRequest, CompletionResult, get_provider, list_providers


class TestModulesAndLoop(unittest.TestCase):
    def test_bundled_modules_valid(self):
        mods = list_modules()
        names = {m["name"] for m in mods}
        self.assertIn("mcp.memory", names)
        self.assertIn("mcp.memory.search", names)
        self.assertIn("mcp.traces", names)
        self.assertIn("a2a.peer", names)
        self.assertIn("skill.catalog", names)
        self.assertIn("skill.list", names)
        self.assertIn("skill.load", names)
        later = [m for m in mods if m["when"] == "later"]
        self.assertTrue(any(m["kind"] == "a2a" for m in later))

    def test_providers_same_shape_as_modules(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENTS_PROVIDERS_DIR"}
        with patch.dict(os.environ, env, clear=True):
            names = {p["name"] for p in list_providers()}
            self.assertIn("openai.default", names)
            self.assertIn("echo", names)
            self.assertIn("scripted.tool", names)
            self.assertNotIn("lmstudio.local", names)
            for p in list_providers():
                self.assertIn("kind", p)
                self.assertIn("verb", p)
                self.assertIn("rests_on", p)
                self.assertEqual(p["verb"], "chat.completions")

    def test_overlay_providers_dir_is_not_in_the_clone(self):
        import os
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp)
            (overlay / "lmstudio.local.json").write_text(
                '{"name":"lmstudio.local","kind":"openai_compat",'
                '"when":"on_request","cadence":"on_request",'
                '"verb":"chat.completions","rests_on":"overlay",'
                '"url":"http://127.0.0.1:1234/v1","key_env":"LM_API_TOKEN"}',
                encoding="utf-8",
            )
            prev = os.environ.get("AGENTS_PROVIDERS_DIR")
            os.environ["AGENTS_PROVIDERS_DIR"] = str(overlay)
            try:
                names = {p["name"] for p in list_providers()}
                self.assertIn("lmstudio.local", names)
                self.assertIn("echo", names)
            finally:
                if prev is None:
                    os.environ.pop("AGENTS_PROVIDERS_DIR", None)
                else:
                    os.environ["AGENTS_PROVIDERS_DIR"] = prev

    def test_echo_provider_complete(self):
        echo = get_provider("echo")
        result = echo.complete(
            CompletionRequest(messages=[{"role": "user", "content": "ping"}])
        )
        self.assertEqual(result.text, "ping")

    def test_assemble_only_rebuilds_trace(self):
        sid = "ses_test"
        with patch(
            "runner.loop._assemble",
            lambda session, limit: [{"role": "user", "content": "hello"}],
        ):
            payload = build_payload(
                sid,
                "next",
                limit=8,
                system="you are a bot",
                start_date="2026-08-31",
                user_id="fabian",
            )
        self.assertEqual(payload[0]["role"], "system")
        self.assertIn("<system_prompt>", payload[0]["content"])
        self.assertIn("you are a bot", payload[0]["content"])
        self.assertIn('start_date="2026-08-31"', payload[0]["content"])
        self.assertNotIn("<clock", payload[0]["content"])
        self.assertTrue(payload[-2]["content"].startswith("<clock"))
        self.assertEqual(payload[-1]["content"], "next")

    def test_loop_list_modules_exits_zero(self):
        rc = loop_main(["--list-modules"])
        self.assertEqual(rc, 0)

    def test_loop_list_providers_exits_zero(self):
        rc = loop_main(["--list-providers"])
        self.assertEqual(rc, 0)

    def _echo_complete(self, deliver: str, channel: str = "telegram"):
        with tempfile.TemporaryDirectory() as tmp:
            traces = Path(tmp) / "traces"
            traces.mkdir()
            ident = Path(tmp) / "identity.json"
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(
                os.environ,
                {
                    "AGENTS_IDENTITY_PATH": str(ident),
                    "AGENTS_TRACES_DIR": str(traces),
                },
            ):
                with redirect_stdout(out), redirect_stderr(err):
                    rc = loop_main(
                        [
                            "--channel",
                            channel,
                            "--user",
                            "looptest",
                            "--new-session",
                            "--message",
                            "ping",
                            "--complete",
                            "--provider",
                            "echo",
                            "--deliver",
                            deliver,
                        ]
                    )
            return rc, out.getvalue(), err.getvalue()

    def test_echo_complete_telegram_buffered_prints_ping(self):
        rc, out, err = self._echo_complete("buffered", channel="telegram")
        self.assertEqual(rc, 0)
        self.assertIn("ping", out)
        self.assertIn(LOOP_TRAILER_MARKER, out)
        trailer_line = out.split(LOOP_TRAILER_MARKER, 1)[1].strip().splitlines()[-1]
        meta = json.loads(trailer_line)
        self.assertTrue(meta.get("session", "").startswith("ses_"))
        self.assertIn("thinking", err.lower())

    def test_echo_complete_stream_writes_ping(self):
        rc, out, _err = self._echo_complete("stream", channel="telegram")
        self.assertEqual(rc, 0)
        self.assertIn("ping", out)


    @unittest.skipUnless(importlib.util.find_spec("agents_memory"), "agents-memory not installed")
    def test_scripted_tool_turn_searches_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = root / "memory"
            note = memory / "notes" / "programming"
            note.mkdir(parents=True)
            (note / "canary.md").write_text(
                "The sandbox canary is amber-47.\n", encoding="utf-8"
            )
            traces = root / "traces"
            traces.mkdir()
            ident = root / "identity.json"
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(
                os.environ,
                {
                    "AGENTS_IDENTITY_PATH": str(ident),
                    "AGENTS_TRACES_DIR": str(traces),
                    "AGENTS_MEMORY_PATH": str(memory),
                    "AGENTS_HOME": str(root / "home"),
                },
            ):
                with redirect_stdout(out), redirect_stderr(err):
                    rc = loop_main(
                        [
                            "--channel",
                            "telegram",
                            "--user",
                            "tooltest",
                            "--new-session",
                            "--message",
                            "canary",
                            "--complete",
                            "--provider",
                            "scripted.tool",
                            "--deliver",
                            "buffered",
                        ]
                    )
            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn("amber-47", out.getvalue())
            self.assertIn("thinking", err.getvalue().lower())

    def test_openai_tools_maps_cordis_surface(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENTS_MODULES_DIR"}
        with patch.dict(os.environ, env, clear=True):
            names = {t["function"]["name"] for t in openai_tools()}
            self.assertEqual(names, {"list_catalog", "load_schema", "call_job"})

    def test_enabled_json_filters_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp)
            (overlay / "enabled.json").write_text("[]", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_MODULES_DIR": str(overlay)}):
                self.assertEqual(openai_tools(), [])

    def test_parse_text_tool_call_xml(self):
        parsed, rest = _parse_text_tool_calls(
            'thinking\n<tool_call>{"name":"call_job","arguments":{"name":"mcp.memory.search","arguments":{"query":"canary"}}}</tool_call>\n'
        )
        self.assertEqual(parsed[0]["function"]["name"], "call_job")
        self.assertIn("canary", parsed[0]["function"]["arguments"])
        self.assertEqual(rest, "thinking")

    @unittest.skipUnless(importlib.util.find_spec("agents_memory"), "agents-memory not installed")
    def test_complete_passes_tools_and_tool_results(self):
        seen: list[CompletionRequest] = []

        class Fake:
            name = "fake"
            kind = "openai_compat"

            def complete(self, req: CompletionRequest) -> CompletionResult:
                seen.append(req)
                if any(m.get("role") == "tool" for m in req.messages):
                    return CompletionResult(text="the token is amber-47")
                return CompletionResult(
                    text="",
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "call_job",
                                "arguments": json.dumps(
                                    {
                                        "name": "mcp.memory.search",
                                        "arguments": {"query": "canary"},
                                    }
                                ),
                            },
                        }
                    ],
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = root / "memory"
            note = memory / "notes" / "programming"
            note.mkdir(parents=True)
            (note / "canary.md").write_text(
                "The sandbox canary is amber-47.\n", encoding="utf-8"
            )
            traces = root / "traces"
            traces.mkdir()
            ident = root / "identity.json"
            out, err = io.StringIO(), io.StringIO()
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("AGENTS_MODULES_DIR", "AGENTS_PROVIDERS_DIR")
            }
            env.update(
                {
                    "AGENTS_IDENTITY_PATH": str(ident),
                    "AGENTS_TRACES_DIR": str(traces),
                    "AGENTS_MEMORY_PATH": str(memory),
                    "AGENTS_HOME": str(root / "home"),
                }
            )
            with patch.dict(os.environ, env, clear=True):
                with patch("runner.loop.get_provider", return_value=Fake()):
                    with redirect_stdout(out), redirect_stderr(err):
                        rc = loop_main(
                            [
                                "--channel",
                                "telegram",
                                "--user",
                                "faketool",
                                "--new-session",
                                "--message",
                                "what is the canary",
                                "--complete",
                                "--provider",
                                "fake",
                                "--deliver",
                                "buffered",
                            ]
                        )
            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn("amber-47", out.getvalue())
            self.assertTrue(seen)
            first = seen[0]
            names = {t["function"]["name"] for t in (first.tools or [])}
            self.assertIn("call_job", names)
            self.assertEqual(len(seen), 2)
            tool_msgs = [m for m in seen[1].messages if m.get("role") == "tool"]
            self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")
            self.assertIn("amber-47", tool_msgs[0]["content"])

    def test_loop_list_tools_exits_zero(self):
        rc = loop_main(["--list-tools"])
        self.assertEqual(rc, 0)

    def test_max_tool_rounds_forces_synthesis(self):
        seen_requests = []

        class EndlessToolProvider:
            def complete(self, req):
                seen_requests.append(req)
                if req.tools:
                    # Model asks for another tool call
                    return CompletionResult(
                        text="",
                        tool_calls=[
                            {
                                "id": f"c_{len(seen_requests)}",
                                "name": "call_job",
                                "arguments": '{"catalog": "skill.catalog"}',
                            }
                        ],
                    )
                # Forced synthesis turn (tools=None)
                return CompletionResult(text="Synthesized answer after tools.", tool_calls=None)

        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.update(
                {
                    "AGENTS_HOME": tmp,
                    "AGENTS_TRACES_DIR": str(Path(tmp) / "traces"),
                    "AGENTS_IDENTITY_PATH": str(Path(tmp) / "identity.json"),
                }
            )
            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, env, clear=True):
                with patch("runner.loop.get_provider", return_value=EndlessToolProvider()):
                    with redirect_stdout(out), redirect_stderr(err):
                        rc = loop_main(
                            [
                                "--channel",
                                "telegram",
                                "--user",
                                "endlesstool",
                                "--new-session",
                                "--message",
                                "search everything",
                                "--complete",
                                "--provider",
                                "fake",
                                "--deliver",
                                "buffered",
                                "--max-tool-rounds",
                                "2",
                            ]
                        )
            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn("Synthesized answer after tools.", out.getvalue())
            # The final request must have had tools=None to force synthesis
            self.assertIsNone(seen_requests[-1].tools)
            notices = [
                m.get("content", "")
                for r in seen_requests
                for m in (r.messages or [])
                if m.get("role") == "user" and "Soft tool-round cap" in str(m.get("content") or "")
            ]
            self.assertFalse(notices)

    def test_loop_emits_last_answer_not_scratch_join(self):
        class DraftThenFinal:
            def complete(self, req):
                if any(m.get("role") == "tool" for m in req.messages):
                    return CompletionResult(text="Forensik final.", tool_calls=None)
                return CompletionResult(
                    text="Forensik-Modus. Erste Runde...",
                    tool_calls=[
                        {
                            "id": "c_draft",
                            "name": "call_job",
                            "arguments": '{"catalog": "skill.catalog"}',
                        }
                    ],
                )

        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.update(
                {
                    "AGENTS_HOME": tmp,
                    "AGENTS_TRACES_DIR": str(Path(tmp) / "traces"),
                    "AGENTS_IDENTITY_PATH": str(Path(tmp) / "identity.json"),
                }
            )
            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, env, clear=True):
                with patch("runner.loop.get_provider", return_value=DraftThenFinal()):
                    with redirect_stdout(out), redirect_stderr(err):
                        rc = loop_main(
                            [
                                "--channel",
                                "telegram",
                                "--user",
                                "draftjoin",
                                "--new-session",
                                "--message",
                                "calendar forensics",
                                "--complete",
                                "--provider",
                                "fake",
                                "--deliver",
                                "buffered",
                            ]
                        )
            self.assertEqual(rc, 0, err.getvalue())
            body = out.getvalue().split(LOOP_TRAILER_MARKER)[0]
            self.assertIn("Forensik final.", body)
            self.assertNotIn("Erste Runde", body)
            self.assertNotIn("Forensik-Modus. Erste Runde...\n\nForensik final.", body)

    def test_loop_protection_and_trace_sealing(self):
        class RepeatThenAnswer:
            def __init__(self):
                self.round = 0

            def complete(self, req: CompletionRequest) -> CompletionResult:
                self.round += 1
                if self.round in (1, 2):
                    return CompletionResult(
                        text="",
                        tool_calls=[
                            {
                                "id": f"call_{self.round}",
                                "name": "call_job",
                                "arguments": '{"name": "skill.catalog"}',
                            }
                        ],
                    )
                return CompletionResult(text="Done repeating.", tool_calls=[])

        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            traces_dir = Path(tmp) / "traces"
            env.update(
                {
                    "AGENTS_HOME": tmp,
                    "AGENTS_TRACES_DIR": str(traces_dir),
                    "AGENTS_IDENTITY_PATH": str(Path(tmp) / "identity.json"),
                }
            )
            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, env, clear=True):
                with patch("runner.loop.get_provider", return_value=RepeatThenAnswer()):
                    with redirect_stdout(out), redirect_stderr(err):
                        rc = loop_main(
                            [
                                "--channel",
                                "local",
                                "--user",
                                "looptest",
                                "--new-session",
                                "--message",
                                "repeat test",
                                "--complete",
                                "--provider",
                                "fake",
                                "--deliver",
                                "buffered",
                                "--seal",
                            ]
                        )
            self.assertEqual(rc, 0, err.getvalue())
            raw = out.getvalue()
            self.assertIn("Done repeating.", raw.split(LOOP_TRAILER_MARKER)[0])
            if importlib.util.find_spec("agents_traces") is None:
                return
            trailer = json.loads(raw.split(LOOP_TRAILER_MARKER)[1].strip())
            self.assertIn("seal", trailer)
            self.assertEqual(len(trailer["seal"]), 64)

    def test_tool_result_is_fenced_denial_is_not(self):
        class ListThenAnswer:
            def __init__(self):
                self.seen = []

            def complete(self, req):
                self.seen.append(req)
                if any(m.get("role") == "tool" for m in req.messages):
                    return CompletionResult(text="listed", tool_calls=None)
                return CompletionResult(
                    text="",
                    tool_calls=[
                        {
                            "id": "c_list",
                            "type": "function",
                            "function": {
                                "name": "call_job",
                                "arguments": json.dumps({"name": "skill.list"}),
                            },
                        }
                    ],
                )

        provider = ListThenAnswer()
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.update(
                {
                    "AGENTS_HOME": tmp,
                    "AGENTS_TRACES_DIR": str(Path(tmp) / "traces"),
                    "AGENTS_IDENTITY_PATH": str(Path(tmp) / "identity.json"),
                    "AGENTS_SKILLS_DIR": str(Path(tmp) / "skills"),
                }
            )
            env.pop("AGENTS_APPROVAL_CMD", None)
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, env, clear=True):
                with patch("runner.loop.get_provider", return_value=provider):
                    with redirect_stdout(out), redirect_stderr(err):
                        rc = loop_main(
                            [
                                "--channel",
                                "local",
                                "--user",
                                "fenceuser",
                                "--new-session",
                                "--message",
                                "skills?",
                                "--complete",
                                "--provider",
                                "fake",
                                "--deliver",
                                "buffered",
                            ]
                        )
            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn("untrusted data", provider.seen[0].messages[0]["content"])
            tool_msgs = [m for m in provider.seen[1].messages if m.get("role") == "tool"]
            self.assertTrue(tool_msgs[0]["content"].startswith('<untrusted_data source="skill.list">'))
            self.assertIn("</untrusted_data>", tool_msgs[0]["content"])

        class DenyAdd:
            def __init__(self):
                self.seen = []

            def complete(self, req):
                self.seen.append(req)
                if any(m.get("role") == "tool" for m in req.messages):
                    return CompletionResult(text="stopped", tool_calls=None)
                return CompletionResult(
                    text="",
                    tool_calls=[
                        {
                            "id": "c_add",
                            "type": "function",
                            "function": {
                                "name": "call_job",
                                "arguments": json.dumps(
                                    {"name": "mcp.memory.add", "arguments": {"fact": "nope"}}
                                ),
                            },
                        }
                    ],
                )

        denier = DenyAdd()
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.update(
                {
                    "AGENTS_HOME": tmp,
                    "AGENTS_TRACES_DIR": str(Path(tmp) / "traces"),
                    "AGENTS_IDENTITY_PATH": str(Path(tmp) / "identity.json"),
                    "AGENTS_SKILLS_DIR": str(Path(tmp) / "skills"),
                    "AGENTS_APPROVAL_MODE": "ask",
                    "AGENTS_APPROVAL_CMD": f"{sys.executable} -c \"import sys; sys.exit(1)\"",
                }
            )
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, env, clear=True):
                with patch("runner.loop.get_provider", return_value=denier):
                    with redirect_stdout(out), redirect_stderr(err):
                        rc = loop_main(
                            [
                                "--channel",
                                "local",
                                "--user",
                                "denyuser",
                                "--new-session",
                                "--message",
                                "remember this",
                                "--complete",
                                "--provider",
                                "fake",
                                "--deliver",
                                "buffered",
                            ]
                        )
            self.assertEqual(rc, 0, err.getvalue())
            tool_msgs = [m for m in denier.seen[1].messages if m.get("role") == "tool"]
            self.assertTrue(tool_msgs[0]["content"].startswith("Denied:"))
            self.assertIn("Do not retry the same tool call.", tool_msgs[0]["content"])
            self.assertNotIn("<untrusted_data", tool_msgs[0]["content"])


if __name__ == "__main__":
    unittest.main()

