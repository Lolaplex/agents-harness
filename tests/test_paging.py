"""Page continuation and config limits."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from runner.cordis_tools import handle_cordis_tool
from runner.retrieve import annotate_clip, read_content, reset_content_state
from runner.settings import bool_setting, int_setting


class TestPaging(unittest.TestCase):
    def setUp(self) -> None:
        reset_content_state()
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"AGENTS_CONTENT_DIR": self._tmp.name}, clear=False)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        reset_content_state()
        self._tmp.cleanup()

    def test_prefix_page_continues_without_a_digest(self) -> None:
        full = "START" + ("m" * 50) + "END"
        shown = annotate_clip(full, limit=8, session_id="ses_page")
        self.assertTrue(shown.startswith("START"))
        self.assertNotIn("END", shown.split("\n<page ", 1)[0])
        self.assertIn("<page ", shown)
        self.assertIn('next_offset="8"', shown)
        page = json.loads(
            handle_cordis_tool(
                "call_job",
                {
                    "function": {
                        "arguments": json.dumps(
                            {"name": "read_content", "arguments": {"next": True, "session_id": "ses_page"}}
                        )
                    }
                },
            )
        )
        self.assertTrue(page["text"].endswith("END"))
        prefix = shown.split("\n<page ", 1)[0]
        self.assertEqual(prefix + page["text"], full)
        self.assertIsNone(page["next_offset"])

    def test_explicit_digest_still_pages(self) -> None:
        full = "ABCDEFGHIJ"
        shown = annotate_clip(full, limit=4, session_id="ses_old")
        digest = shown.split('digest="', 1)[1].split('"', 1)[0]
        page = read_content("ses_old", digest, offset=4, limit=4)
        self.assertEqual(page["text"], "EFGH")
        self.assertTrue(page["truncated"])
        self.assertEqual(page["total_chars"], len(full))


class TestSettings(unittest.TestCase):
    def test_env_wins_and_invalid_env_uses_the_default(self) -> None:
        with patch.dict(os.environ, {"AGENTS_PAGE_CHARS": "100"}, clear=False):
            with patch("runner.settings.config_value", return_value=5):
                self.assertEqual(int_setting("AGENTS_PAGE_CHARS", "page_chars", 9), 100)
        with patch.dict(os.environ, {"AGENTS_PAGE_CHARS": "nope"}, clear=False):
            with patch("runner.settings.config_value", return_value=5):
                self.assertEqual(int_setting("AGENTS_PAGE_CHARS", "page_chars", 9), 9)

    def test_config_used_when_env_is_unset(self) -> None:
        env = {key: value for key, value in os.environ.items() if key != "AGENTS_PAGE_CHARS"}
        with patch.dict(os.environ, env, clear=True):
            with patch("runner.settings.config_value", return_value=64):
                self.assertEqual(int_setting("AGENTS_PAGE_CHARS", "page_chars", 9), 64)

    def test_bool_config(self) -> None:
        env = {key: value for key, value in os.environ.items() if key != "AGENTS_PROJECT_CONTEXT"}
        with patch.dict(os.environ, env, clear=True):
            with patch("runner.settings.config_value", return_value=False):
                self.assertFalse(bool_setting("AGENTS_PROJECT_CONTEXT", "project_context", True))


if __name__ == "__main__":
    unittest.main()
