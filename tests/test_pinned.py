"""Pinned verbs block: hot call_job examples in the system prompt."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from runner.cordis_tools import _arguments_to_argv
from runner.helpjson import clear_help_cache, with_discovered_commands
from runner.modules import find_module
from runner.pinned import (
    CLIP_MARKER,
    pinned_entries,
    pinned_verbs_prompt_block,
)


def _terminal_ready() -> bool:
    mod = find_module("mcp.terminal")
    if mod is None:
        return False
    commands = with_discovered_commands(mod).get("commands")
    return isinstance(commands, dict) and "write" in commands and "edit" in commands


EXPECTED_ARGV = {
    "list": ["list"],
    "read": ["read", "README.md"],
    "grep": ["grep", "TODO", "--glob", "*"],
    "stat": ["stat", "README.md"],
    "write": ["write", "notes.txt", "--content", "hello world"],
    "edit": ["edit", "notes.txt", "--old", "hello world", "--new", "hello there"],
    "run": ["run", "--", "python", "-m", "unittest"],
}


class TestPinnedBlock(unittest.TestCase):
    def setUp(self) -> None:
        clear_help_cache()
        os.environ.pop("AGENTS_PINNED_VERBS", None)

    def tearDown(self) -> None:
        clear_help_cache()
        os.environ.pop("AGENTS_PINNED_VERBS", None)

    @unittest.skipUnless(_terminal_ready(), "agents-terminal with write/edit is not installed")
    def test_hot_verbs_are_pinned_with_their_class(self) -> None:
        block = pinned_verbs_prompt_block()
        self.assertIn("write [mutating]", block)
        self.assertIn("edit [mutating]", block)
        self.assertIn("read [read-only]", block)
        self.assertIn("grep [read-only]", block)

    @unittest.skipUnless(_terminal_ready(), "agents-terminal with write/edit is not installed")
    def test_every_example_round_trips_through_the_real_mapper(self) -> None:
        block = pinned_verbs_prompt_block()
        found = find_module("mcp.terminal")
        assert found is not None
        mod = with_discovered_commands(found)
        seen: set[str] = set()
        for line in block.splitlines():
            if not line.startswith("- "):
                continue
            start = line.find("{")
            self.assertGreaterEqual(start, 0, line)
            call = json.loads(line[start:])
            self.assertEqual(call["name"], "mcp.terminal")
            command = call["arguments"]["command"]
            self.assertEqual(
                _arguments_to_argv(mod, call["arguments"]),
                EXPECTED_ARGV[command],
                line,
            )
            seen.add(command)
        self.assertEqual(seen, set(EXPECTED_ARGV))

    @unittest.skipUnless(_terminal_ready(), "agents-terminal with write/edit is not installed")
    def test_edit_example_uses_flags_because_stdin_is_not_wired(self) -> None:
        block = pinned_verbs_prompt_block()
        self.assertIn('"old":"hello world"', block)
        self.assertIn('"new":"hello there"', block)
        self.assertNotIn("stdin_json", block)
        self.assertIn("does not pass stdin", block)

    def test_env_disables_the_block(self) -> None:
        with patch.dict(os.environ, {"AGENTS_PINNED_VERBS": "0"}):
            self.assertEqual(pinned_verbs_prompt_block(), "")

    def test_unpinned_module_is_skipped(self) -> None:
        mod = {
            "name": "mcp.unpinned",
            "commands": {"read": {"side_effects": "read-only"}},
        }
        with patch("runner.pinned.list_modules", return_value=[mod]):
            self.assertEqual(pinned_verbs_prompt_block(), "")

    def test_broken_help_json_reports_unavailable(self) -> None:
        mod = {
            "name": "mcp.broken",
            "kind": "mcp",
            "when": "on_request",
            "cadence": "on_request",
            "verb": "python -m broken_tool",
            "rests_on": "test",
            "pin": True,
            "commands_from": "help-json",
            "mutates": True,
        }
        with patch("runner.pinned.list_modules", return_value=[mod]), patch(
            "runner.helpjson.subprocess.run", side_effect=OSError("missing")
        ):
            block = pinned_verbs_prompt_block()
        self.assertIn("pinned: mcp.broken unavailable", block)
        self.assertNotIn("Traceback", block)

    def test_length_limit_clips_and_marks(self) -> None:
        commands = {
            f"cmd{i}": {
                "side_effects": "read-only",
                "parameters": {"type": "object", "properties": {}, "required": []},
            }
            for i in range(40)
        }
        mod = {"name": "mcp.many", "pin": True, "commands": commands}
        with patch("runner.pinned.list_modules", return_value=[mod]):
            block = pinned_verbs_prompt_block(limit=400)
        self.assertLessEqual(len(block), 400)
        self.assertIn(CLIP_MARKER, block)

    def test_augment_system_appends_the_block_after_skills(self) -> None:
        from runner.loop import _augment_system

        with patch("runner.loop.memory_rules_prompt_block", return_value=""), patch(
            "runner.loop.skills_prompt_block", return_value="SKILLS"
        ), patch("runner.loop.pinned_verbs_prompt_block", return_value="PINNED"):
            text = _augment_system("base")
        self.assertLess(text.index("SKILLS"), text.index("PINNED"))

    def test_entries_expose_the_call_and_class(self) -> None:
        mod = {
            "name": "mcp.demo",
            "pin": True,
            "commands": {
                "read": {
                    "side_effects": "read-only",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            },
        }
        with patch("runner.pinned.list_modules", return_value=[mod]):
            rows = pinned_entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["command"], "read")
        self.assertEqual(rows[0]["side_effects"], "read-only")
        self.assertEqual(rows[0]["arguments"], {"command": "read", "path": "README.md"})


if __name__ == "__main__":
    unittest.main()
