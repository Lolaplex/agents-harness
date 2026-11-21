import io
import json
import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from runner.providers import (
    CompletionRequest,
    OpenAICompatProvider,
    consume_sse,
    get_provider,
    load_provider,
)


class TestConsumeSse(unittest.TestCase):
    def test_folds_content_deltas(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"sand"}}]}',
            'data: {"choices":[{"delta":{"content":"box-ok"}}]}',
            "data: [DONE]",
        ]
        text, tools, meta = consume_sse(lines)
        self.assertEqual(text, "sandbox-ok")
        self.assertIsNone(tools)
        self.assertEqual(meta["sse_chunks"], 2)

    def test_folds_tool_call_argument_pieces(self):
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"search_memory","arguments":"{"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]}}]}',
            "data: [DONE]",
        ]
        text, tools, _ = consume_sse(lines)
        self.assertEqual(text, "")
        self.assertEqual(tools[0]["function"]["name"], "search_memory")
        self.assertEqual(tools[0]["function"]["arguments"], "{}")

    def test_folds_message_tool_calls(self):
        lines = [
            'data: {"choices":[{"message":{"tool_calls":[{"id":"c1","function":{"name":"mcp_memory_search","arguments":"{\\"query\\":\\"canary\\"}"}}]}}]}',
            "data: [DONE]",
        ]
        text, tools, _ = consume_sse(lines)
        self.assertEqual(text, "")
        self.assertEqual(tools[0]["function"]["name"], "mcp_memory_search")
        self.assertIn("canary", tools[0]["function"]["arguments"])

    def test_preserves_thought_signature_extras(self):
        # Gemini attaches extra_content.google.thought_signature to streamed
        # functionCall parts; dropping it makes the follow-up request 400.
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"list_catalog","arguments":"{}"},"extra_content":{"google":{"thought_signature":"sig-abc"}}}]}}]}',
            "data: [DONE]",
        ]
        text, tools, _ = consume_sse(lines)
        self.assertEqual(
            tools[0]["extra_content"]["google"]["thought_signature"], "sig-abc"
        )


class _FakeResponse:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestProviderKeyWarnings(unittest.TestCase):
    def test_warns_on_plaintext_api_key_and_preserves_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp)
            manifest = {
                "name": "legacy.prov",
                "kind": "openai_compat",
                "when": "on_request",
                "cadence": "on_request",
                "verb": "chat.completions",
                "rests_on": "test",
                "expected_exit": 0,
                "url": "https://api.example/v1",
                "model": "gpt-test",
                "api_key": "sk-plaintext-legacy",
            }
            mpath = pdir / "legacy.json"
            mpath.write_text(json.dumps(manifest), encoding="utf-8")

            with warnings.catch_warnings(record=True) as caught_load:
                warnings.simplefilter("always")
                loaded = load_provider(mpath)
            self.assertEqual(loaded["api_key"], "sk-plaintext-legacy")
            self.assertEqual(len(caught_load), 1)
            self.assertTrue(issubclass(caught_load[0].category, UserWarning))
            self.assertIn("api_key", str(caught_load[0].message))
            self.assertIn("key_env", str(caught_load[0].message))

            with warnings.catch_warnings(record=True) as caught_get:
                warnings.simplefilter("always")
                prov = get_provider("legacy.prov", providers_dir=pdir)
            self.assertEqual(len(caught_get), 1)

            seen_auth: list[str] = []

            def fake_urlopen(req, timeout=None):
                seen_auth.append(req.get_header("Authorization") or "")
                return _FakeResponse(
                    json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")
                )

            with (
                patch.dict(os.environ, {"AGENTS_HOST_DIR": tmp, "LLM_API_KEY": ""}, clear=False),
                patch("urllib.request.urlopen", side_effect=fake_urlopen),
            ):
                res = prov.complete(
                    CompletionRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        extra={"stream": False},
                    )
                )
            self.assertEqual(res.text, "ok")
            self.assertEqual(seen_auth, ["Bearer sk-plaintext-legacy"])

    def test_no_warning_for_key_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp)
            manifest = {
                "name": "clean.prov",
                "kind": "openai_compat",
                "when": "on_request",
                "cadence": "on_request",
                "verb": "chat.completions",
                "rests_on": "test",
                "expected_exit": 0,
                "url": "https://api.example/v1",
                "model": "gpt-test",
                "key_env": "MY_PROVIDER_KEY",
            }
            mpath = pdir / "clean.json"
            mpath.write_text(json.dumps(manifest), encoding="utf-8")

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                load_provider(mpath)
                get_provider("clean.prov", providers_dir=pdir)
                OpenAICompatProvider({"name": "direct.clean", "key_env": "MY_KEY", "api_key": ""})
            self.assertEqual(caught, [])

    def test_direct_openaicompat_warns_on_plaintext_api_key(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            OpenAICompatProvider({"name": "direct.legacy", "api_key": "sk-direct"})
        self.assertEqual(len(caught), 1)
        self.assertTrue(issubclass(caught[0].category, UserWarning))
        self.assertIn("api_key", str(caught[0].message))


if __name__ == "__main__":
    unittest.main()
