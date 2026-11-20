"""vision.analyze module: one-shot image analysis through a vision provider."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner import vision_analyze


class _FakeProvider:
    name = "fake.vision"
    kind = "echo"

    def __init__(self, manifest=None):
        self.manifest = manifest or {"name": "fake.vision", "vision": True}
        self.last = None

    def complete(self, req):
        self.last = req
        return type("R", (), {"text": "a cat on a mat", "tool_calls": None, "raw": {}})()


class TestVisionAnalyze(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.img = Path(self.tmp.name) / "pic.png"
        self.img.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")

    def test_help_json_and_manifests(self):
        from runner import modules, helpjson

        mod = [m for m in modules.list_modules() if m["name"] == "vision.analyze"][0]
        self.assertEqual(modules.openai_tool_name(mod), "vision_analyze")
        mod = helpjson.with_discovered_commands(mod)
        self.assertEqual(mod["commands"]["analyze"]["side_effects"], "read-only")
        argv = helpjson.declared_argv(mod, {"image": "x.png", "prompt": "cat?"})
        self.assertEqual(argv[:3], ["analyze", "x.png", "--prompt"])

    def test_analyze_sends_image_part(self):
        fake = _FakeProvider()
        with patch.object(vision_analyze, "get_provider", return_value=fake):
            out = vision_analyze.analyze(str(self.img), prompt="what animal")
        self.assertTrue(out["ok"])
        content = fake.last.messages[0]["content"]
        self.assertEqual(content[0]["text"], "what animal")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(out["text"], "a cat on a mat")

    def test_no_vision_provider_refuses(self):
        fake = _FakeProvider(manifest={"name": "plain"})
        env = {k: v for k, v in os.environ.items() if k != "AGENTS_VISION"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(vision_analyze, "get_provider", return_value=fake):
                with self.assertRaises(ValueError) as ctx:
                    vision_analyze.analyze(str(self.img))
        self.assertIn("no vision", str(ctx.exception))

    def test_env_flag_allows_any_provider(self):
        fake = _FakeProvider(manifest={"name": "plain"})
        with patch.dict(os.environ, {"AGENTS_VISION": "1"}):
            with patch.object(vision_analyze, "get_provider", return_value=fake):
                out = vision_analyze.analyze(str(self.img))
        self.assertTrue(out["ok"])

    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            vision_analyze.analyze(str(self.tmp.name))
        bad = Path(self.tmp.name) / "x.bmp"
        bad.write_bytes(b"BM")
        with self.assertRaises(ValueError):
            vision_analyze.analyze(str(bad))
        empty = Path(self.tmp.name) / "e.png"
        empty.write_bytes(b"")
        with self.assertRaises(ValueError):
            vision_analyze.analyze(str(empty))

    def test_cli(self):
        fake = _FakeProvider()
        with patch.object(vision_analyze, "get_provider", return_value=fake):
            import io, contextlib

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = vision_analyze.main(["analyze", str(self.img), "--prompt", "hi"])
            self.assertEqual(rc, 0)
            self.assertTrue(json.loads(buf.getvalue())["ok"])
        self.assertEqual(vision_analyze.main(["analyze"]), 2)


if __name__ == "__main__":
    unittest.main()
