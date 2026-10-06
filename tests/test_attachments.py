"""runner.loop --attach vision parts vs file-path text."""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.attachments import render_user_content, vision_enabled
from runner.loop import build_payload, main as loop_main


class TestAttachments(unittest.TestCase):
    def test_vision_flag_and_provider_capability(self):
        with patch.dict(os.environ, {"AGENTS_VISION": "1"}):
            self.assertTrue(vision_enabled(None))
        with patch.dict(os.environ, {"AGENTS_VISION": "0"}):
            self.assertFalse(vision_enabled({"vision": True}))
        env = {k: v for k, v in os.environ.items() if k != "AGENTS_VISION"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(vision_enabled(None))
            self.assertTrue(vision_enabled({"vision": True}))
            self.assertTrue(vision_enabled({"capabilities": ["vision"]}))

    def test_render_path_text_or_image_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "shot.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nrest")
            note = Path(tmp) / "notes.txt"
            note.write_text("hello", encoding="utf-8")
            plain = render_user_content("see", [str(image), str(note)], vision=False)
            self.assertIsInstance(plain, str)
            self.assertIn(str(image), plain)
            self.assertIn("Attached files:", plain)
            rich = render_user_content("see", [str(image), str(note)], vision=True)
            self.assertIsInstance(rich, list)
            self.assertEqual(rich[0]["text"], "see")
            self.assertEqual(rich[1]["type"], "image_url")
            encoded = rich[1]["image_url"]["url"]
            self.assertTrue(encoded.startswith("data:image/png;base64,"))
            self.assertEqual(base64.b64decode(encoded.split(",", 1)[1]), image.read_bytes())
            self.assertIn(str(note), rich[2]["text"])

    def test_assemble_only_attach(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "shot.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nrest")
            out = io.StringIO()
            env = {
                "AGENTS_VISION": "1",
                "AGENTS_IDENTITY_PATH": str(Path(tmp) / "identity.json"),
                "AGENTS_TRACES_DIR": str(Path(tmp) / "traces"),
                "AGENTS_SKILLS_DIR": str(Path(tmp) / "no-skills"),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch("sys.stdout", out):
                    rc = loop_main(
                        [
                            "--user",
                            "attachtest",
                            "--new-session",
                            "--message",
                            "look",
                            "--attach",
                            str(image),
                            "--assemble-only",
                            "--provider",
                            "echo",
                        ]
                    )
            self.assertEqual(rc, 0)
            payload = json.loads(out.getvalue())
            user = payload["messages"][-1]
            self.assertIsInstance(user["content"], list)
            self.assertEqual(user["content"][1]["type"], "image_url")
            system = payload["messages"][0]["content"]
            self.assertIn("untrusted_data", system)

    def test_build_payload_without_vision_is_path_text(self):
        messages = build_payload(
            "ses_a",
            "hi",
            system="bot",
            include_clock=False,
            attachments=["/tmp/pic.png"],
            vision=False,
        )
        self.assertIn("/tmp/pic.png", messages[-1]["content"])
        self.assertIn("untrusted data", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
