import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from runner.loop import build_payload, main as loop_main
from runner.modules import list_modules
from runner.providers import CompletionRequest, get_provider, list_providers


class TestModulesAndLoop(unittest.TestCase):
    def test_bundled_modules_valid(self):
        mods = list_modules()
        names = {m["name"] for m in mods}
        self.assertIn("mcp.memory", names)
        self.assertIn("mcp.traces", names)
        self.assertIn("a2a.peer", names)
        self.assertIn("skill.catalog", names)
        later = [m for m in mods if m["when"] == "later"]
        self.assertTrue(any(m["kind"] == "a2a" for m in later))

    def test_providers_same_shape_as_modules(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENTS_PROVIDERS_DIR"}
        with patch.dict(os.environ, env, clear=True):
            names = {p["name"] for p in list_providers()}
            self.assertIn("openai.default", names)
            self.assertIn("echo", names)
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
        self.assertEqual(out.strip(), "ping")
        self.assertIn("thinking", err.lower())

    def test_echo_complete_stream_writes_ping(self):
        rc, out, _err = self._echo_complete("stream", channel="telegram")
        self.assertEqual(rc, 0)
        self.assertIn("ping", out)


if __name__ == "__main__":
    unittest.main()
