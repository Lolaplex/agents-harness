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


def _http_error(code: int, retry_after: str | None = None):
    import email.message
    import urllib.error

    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://api.example/v1/chat/completions", code, "err", hdrs,
        io.BytesIO(b'{"error":"busy"}'))


class TestProviderRetries(unittest.TestCase):
    def _prov(self, **extra):
        manifest = {"name": "retry.prov", "url": "https://api.example/v1",
                    "model": "m", "key_env": "RETRY_TEST_KEY"}
        manifest.update(extra)
        return OpenAICompatProvider(manifest)

    def _run(self, prov, outcomes):
        calls = []
        sleeps = []
        statuses = []

        def fake_urlopen(req, timeout=None):
            calls.append(json.loads(req.data.decode("utf-8")).get("model"))
            item = outcomes[len(calls) - 1]
            if isinstance(item, Exception):
                raise item
            return _FakeResponse(json.dumps(
                {"choices": [{"message": {"content": item}}]}).encode("utf-8"))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                patch("runner.providers._sleep", side_effect=sleeps.append):
            try:
                res = prov.complete(CompletionRequest(
                    messages=[{"role": "user", "content": "hi"}],
                    extra={"stream": False}, on_status=statuses.append))
            except RuntimeError as exc:
                res = exc
        return res, calls, sleeps, statuses

    def test_retries_transient_then_succeeds(self):
        res, calls, sleeps, statuses = self._run(
            self._prov(), [_http_error(429), _http_error(503), "ok"])
        self.assertEqual(res.text, "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertTrue(any("retry 1/2" in s for s in statuses))

    def test_gives_up_after_retries(self):
        res, calls, sleeps, _ = self._run(
            self._prov(), [_http_error(502) for _ in range(3)])
        self.assertIsInstance(res, RuntimeError)
        self.assertIn("HTTP 502", str(res))
        self.assertEqual(len(calls), 3)

    def test_non_transient_not_retried(self):
        res, calls, sleeps, _ = self._run(
            self._prov(), [_http_error(400), "never"])
        self.assertIsInstance(res, RuntimeError)
        self.assertIn("HTTP 400", str(res))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_retry_after_header_capped(self):
        res, calls, sleeps, _ = self._run(
            self._prov(), [_http_error(429, "3"), _http_error(429, "600"), "ok"])
        self.assertEqual(res.text, "ok")
        self.assertEqual(sleeps, [3.0, 10.0])

    def test_manifest_retries_zero_disables(self):
        res, calls, _, _ = self._run(self._prov(retries=0), [_http_error(503), "x"])
        self.assertIsInstance(res, RuntimeError)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()


class TestWorkspaceProviderOverlays(unittest.TestCase):
    """agents-harness#37: <host>/providers/ws-<id> layering."""

    def test_ws_overlay_overrides_host(self):
        import json
        import os
        from unittest.mock import patch
        from agents_workspace import Host
        from runner.providers import find_provider

        with tempfile.TemporaryDirectory() as tmp:
            host = Host.open(tmp)
            host.create("ws-a")
            host.create("ws-b")
            (host.dir / "providers").mkdir(parents=True)
            (host.dir / "providers" / "custom.json").write_text(json.dumps(
                {"name": "custom", "kind": "echo", "verb": "true", "cadence": "on_request", "rests_on": []}), encoding="utf-8")
            ws_dir = host.dir / "providers" / "ws-ws-a"
            ws_dir.mkdir(parents=True)
            (ws_dir / "custom.json").write_text(json.dumps(
                {"name": "custom", "kind": "scripted", "verb": "true", "cadence": "on_request", "rests_on": [], "tool_name": "ws-a-tool"}), encoding="utf-8")
            env = {"AGENTS_HOST_DIR": tmp, "AGENTS_WORKSPACE": "ws-a"}
            with patch.dict(os.environ, env, clear=False):
                row = find_provider("custom")
                self.assertEqual(row["kind"], "scripted")
            env = {"AGENTS_HOST_DIR": tmp, "AGENTS_WORKSPACE": "ws-b"}
            with patch.dict(os.environ, env, clear=False):
                row = find_provider("custom")
                self.assertEqual(row["kind"], "echo")

    def test_ws_local_providers_dir_ignored(self):
        import json
        import os
        from unittest.mock import patch
        from agents_workspace import Host
        from runner.providers import find_provider

        with tempfile.TemporaryDirectory() as tmp:
            host = Host.open(tmp)
            ws = host.create("ws-a")
            poison = ws.dir / "providers"
            poison.mkdir(parents=True)
            (poison / "evil.json").write_text(json.dumps(
                {"name": "evil", "kind": "echo", "verb": "true", "cadence": "on_request", "rests_on": []}), encoding="utf-8")
            env = {"AGENTS_HOST_DIR": tmp, "AGENTS_WORKSPACE": "ws-a"}
            with patch.dict(os.environ, env, clear=False):
                self.assertIsNone(find_provider("evil"))
