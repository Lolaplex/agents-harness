"""Hard-rules system prefix: soft dep, cache, disable switch."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from runner import memory_rules as mr
from runner.fence import FENCE_SYSTEM_NOTE


class TestMemoryRules(unittest.TestCase):
    def setUp(self) -> None:
        mr.clear_cache()
        self._saved = os.environ.get("AGENTS_MEMORY_RULES")
        os.environ.pop("AGENTS_MEMORY_RULES", None)

    def tearDown(self) -> None:
        mr.clear_cache()
        if self._saved is None:
            os.environ.pop("AGENTS_MEMORY_RULES", None)
        else:
            os.environ["AGENTS_MEMORY_RULES"] = self._saved

    def test_disabled_env(self) -> None:
        os.environ["AGENTS_MEMORY_RULES"] = "0"
        with mock.patch.object(mr, "_via_import", return_value="<memory_rules>\n- x\n</memory_rules>"):
            self.assertEqual(mr.memory_rules_prompt_block(), "")

    def test_import_path_prefixes(self) -> None:
        block = "<memory_rules>\n- Keep secrets local\n</memory_rules>"
        with mock.patch.object(mr, "_via_import", return_value=block):
            with mock.patch.object(mr, "_via_cli") as cli:
                out = mr.memory_rules_prompt_block()
                cli.assert_not_called()
        self.assertIn("Hard rules from local agent memory", out)
        self.assertIn(block, out)
        with mock.patch.object(mr, "_via_import") as imp:
            again = mr.memory_rules_prompt_block()
            imp.assert_not_called()
        self.assertEqual(again, out)

    def test_cli_fallback_when_import_missing(self) -> None:
        block = "<memory_rules>\n- via cli\n</memory_rules>"
        with mock.patch.object(mr, "_via_import", return_value=None):
            with mock.patch.object(mr, "_via_cli", return_value=block) as cli:
                out = mr.memory_rules_prompt_block("demo")
                cli.assert_called_once_with("demo")
        self.assertIn(block, out)

    def test_soft_fail_empty(self) -> None:
        with mock.patch.object(mr, "_via_import", return_value=None):
            with mock.patch.object(mr, "_via_cli", return_value=""):
                self.assertEqual(mr.memory_rules_prompt_block(), "")

    def test_augment_system_includes_rules(self) -> None:
        from runner.loop import _augment_system

        block = "<memory_rules>\n- Be brief\n</memory_rules>"
        with mock.patch.object(mr, "render_memory_rules", return_value=block):
            with mock.patch("runner.loop.skills_prompt_block", return_value=""):
                out = _augment_system("You are test.", project="")
        self.assertIn("You are test.", out)
        self.assertIn(FENCE_SYSTEM_NOTE, out)
        self.assertIn("Be brief", out)

    def test_calendar_update_delete_argv(self) -> None:
        from runner.cordis_tools import _arguments_to_argv

        args = _arguments_to_argv(
            {"name": "mcp.calendar.update"},
            {"href": "/ev", "etag": '"1"', "summary": "Hi"},
        )
        self.assertEqual(args[:4], ["--href", "/ev", "--etag", '"1"'])
        self.assertIn("--summary", args)

        deleted = _arguments_to_argv(
            {"name": "mcp.calendar.delete"},
            {"href": "/ev", "etag": "x"},
        )
        self.assertEqual(deleted, ["--href", "/ev", "--etag", "x"])
        self.assertEqual(_arguments_to_argv({"name": "mcp.calendar.calendars"}, {}), [])


if __name__ == "__main__":
    unittest.main()
