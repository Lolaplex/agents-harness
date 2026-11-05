"""Help-json side effects from a fake manifest. Names are not a class."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest.mock import patch

from runner.approval import call_is_mutator, gate_module, module_is_mutator, module_is_read_only, needs_approval
from runner.cordis_tools import _arguments_to_argv, handle_cordis_tool
from runner.helpjson import (
    MUTATING,
    READ_ONLY,
    call_effects,
    classify_spec,
    clear_help_cache,
    declared_argv,
    with_discovered_commands,
)
from runner.mcp_client import modules_from_descriptors
from runner.modules import find_module


FAKE = {
    "name": "mcp.widget",
    "kind": "mcp",
    "mutates": True,
    "commands": {
        "list": {
            "side_effects": "read-only",
            "mutates": False,
            "annotations": {"readOnlyHint": True},
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []},
        },
        "read": {
            "mutates": False,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
        "grep": {
            "side_effects": "read-only",
            "aliases": ["search"],
            "annotations": {"readOnlyHint": True},
        },
        "stat": {
            "side_effects": "read-only",
            "mutates": True,
            "annotations": {"readOnlyHint": False},
        },
        "run": {
            "side_effects": "mutating",
            "mutates": True,
            "annotations": {"readOnlyHint": False},
            "usage": "widget run -- <argv...>",
            "parameters": {
                "type": "object",
                "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
                "required": ["argv"],
            },
        },
        "bare": {"description": "no side-effect metadata"},
        "delete": {"side_effects": "read-only"},
    },
}


class TestClassify(unittest.TestCase):
    def test_precedence(self) -> None:
        self.assertEqual(
            classify_spec(
                {
                    "side_effects": "read-only",
                    "mutates": True,
                    "annotations": {"readOnlyHint": False},
                }
            ),
            READ_ONLY,
        )
        self.assertEqual(
            classify_spec({"mutates": False, "annotations": {"readOnlyHint": False}}),
            READ_ONLY,
        )
        self.assertEqual(classify_spec({"annotations": {"readOnlyHint": True}}), READ_ONLY)
        self.assertEqual(classify_spec({"readOnlyHint": False}), MUTATING)
        self.assertEqual(classify_spec({"description": "list"}), MUTATING)
        self.assertIsNone(classify_spec({"description": "list"}, default=None))

    def test_fake_manifest_commands(self) -> None:
        self.assertEqual(call_effects(FAKE, {"argv": ["list", "."]}), READ_ONLY)
        self.assertEqual(call_effects(FAKE, {"argv": ["read", "a.txt"]}), READ_ONLY)
        self.assertEqual(call_effects(FAKE, {"argv": ["grep", "pat"]}), READ_ONLY)
        self.assertEqual(call_effects(FAKE, {"argv": ["search", "pat"]}), READ_ONLY)
        self.assertEqual(call_effects(FAKE, {"command": "stat"}), READ_ONLY)
        self.assertEqual(call_effects(FAKE, {"argv": ["run", "--", "rm", "x"]}), MUTATING)
        self.assertEqual(call_effects(FAKE, {"argv": ["bare"]}), MUTATING)
        self.assertEqual(call_effects(FAKE, {"argv": ["delete", "x"]}), READ_ONLY)
        self.assertIsNone(call_effects(FAKE, {"command": "git status"}))
        self.assertIsNone(call_effects(FAKE, {"argv": ["echo", "hi"]}))

    def test_old_modules_keep_their_flags(self) -> None:
        search = find_module("mcp.memory.search")
        terminal = find_module("mcp.terminal")
        self.assertIsNotNone(search)
        self.assertIsNotNone(terminal)
        assert search is not None and terminal is not None
        self.assertTrue(module_is_read_only(search))
        self.assertFalse(module_is_mutator(search))
        self.assertTrue(module_is_mutator(terminal))
        self.assertFalse(module_is_read_only(terminal))
        bare = {"name": "mcp.custom.search"}
        self.assertTrue(module_is_mutator(bare))
        self.assertFalse(module_is_read_only(bare))
        self.assertTrue(module_is_mutator({"name": "mcp.custom.unknown"}))
        self.assertTrue(call_is_mutator(terminal, {"argv": ["list", "."]}))
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("AGENTS_APPROVAL_CMD", "AGENTS_APPROVAL_MODE")
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(needs_approval(bare))

    def test_bundled_manifests_declare_effects(self) -> None:
        from pathlib import Path

        folder = Path(__file__).resolve().parents[1] / "runner" / "modules"
        for path in sorted(folder.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNotNone(
                classify_spec(data, default=None),
                path.name,
            )

    def test_unresolved_call_is_mutating_without_scanning_arguments(self) -> None:
        from runner.loop import _tool_call_mutates

        quiet = json.dumps({"name": "mcp.missing.tool", "arguments": {"query": "hello"}})
        self.assertTrue(_tool_call_mutates("call_job", quiet))
        named = json.dumps(
            {"name": "mcp.memory.search", "arguments": {"query": "delete everything"}}
        )
        self.assertFalse(_tool_call_mutates("call_job", named))

    def test_approval_uses_the_command_not_the_module_flag(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENTS_APPROVAL_CMD": "false", "AGENTS_APPROVAL_MODE": "ask"},
            clear=False,
        ):
            self.assertFalse(needs_approval(FAKE, {"argv": ["list", "."]}))
            self.assertFalse(needs_approval(FAKE, {"argv": ["search", "q"]}))
            self.assertTrue(needs_approval(FAKE, {"argv": ["run", "--", "rm"]}))
            self.assertTrue(needs_approval(FAKE, {"argv": ["bare"]}))
            self.assertTrue(needs_approval(FAKE, {"command": "git status"}))
            allowed, _msg = gate_module(FAKE, {"argv": ["stat"]})
            self.assertTrue(allowed)
            denied, why = gate_module(FAKE, {"argv": ["run", "--", "rm", "-rf"]})
        self.assertFalse(denied)
        self.assertIn("declined", why)
        with patch.dict(
            os.environ,
            {"AGENTS_APPROVAL_CMD": "false", "AGENTS_APPROVAL_MODE": "strict"},
            clear=False,
        ):
            self.assertFalse(needs_approval(FAKE, {"command_name": "read", "path": "a"}))
            self.assertTrue(needs_approval(FAKE, {"argv": ["bare"]}))

    def test_declared_argv_passes_a_named_command_through(self) -> None:
        self.assertEqual(declared_argv(FAKE, {"argv": ["list", "."]}), ["list", "."])
        self.assertEqual(
            declared_argv(FAKE, {"command_name": "read", "path": "a.txt", "offset": 2}),
            ["read", "a.txt", "--offset", "2"],
        )
        self.assertEqual(_arguments_to_argv(FAKE, {"argv": ["echo", "hi"]}), ["echo", "hi"])
        terminal = {
            "name": "mcp.terminal",
            "commands": FAKE["commands"],
        }
        self.assertEqual(_arguments_to_argv(terminal, {"argv": ["list", "src"]}), ["list", "src"])
        self.assertEqual(
            _arguments_to_argv(terminal, {"argv": ["echo", "hi"]}),
            ["run", "--", "echo", "hi"],
        )
        self.assertEqual(
            _arguments_to_argv(terminal, {"command_name": "run", "argv": ["echo", "hi"]}),
            ["run", "--", "echo", "hi"],
        )

    def test_descriptor_trusts_side_effects_over_hint(self) -> None:
        rows = modules_from_descriptors(
            "demo",
            [
                {
                    "name": "list",
                    "description": "list",
                    "inputSchema": {"type": "object", "properties": {}},
                    "side_effects": "read-only",
                    "mutates": True,
                    "readOnlyHint": False,
                    "annotations": {"readOnlyHint": False},
                }
            ],
        )
        self.assertFalse(rows[0]["mutates"])
        self.assertEqual(rows[0]["side_effects"], "read-only")
        self.assertTrue(rows[0]["readOnlyHint"])

    def test_help_json_discovery_merges_commands(self) -> None:
        clear_help_cache()
        doc = {
            "commands": {
                "list": {"side_effects": "read-only", "mutates": False},
                "run": {"side_effects": "mutating"},
            }
        }

        class Proc:
            returncode = 0
            stdout = json.dumps(doc)
            stderr = ""

        mod = {
            "name": "mcp.example",
            "verb": "python -m example_tool",
            "commands_from": "help-json",
            "mutates": True,
        }
        with patch("runner.helpjson.subprocess.run", return_value=Proc()):
            merged = with_discovered_commands(mod)
        self.assertEqual(call_effects(merged, {"argv": ["list"]}), READ_ONLY)
        self.assertEqual(call_effects(merged, {"argv": ["run", "--", "x"]}), MUTATING)
        self.assertTrue(call_is_mutator(mod, {"argv": ["list"]}))
        clear_help_cache()
        with patch("runner.helpjson.subprocess.run", side_effect=OSError("missing")):
            self.assertIs(with_discovered_commands(mod), mod)

    def test_call_job_gates_discovered_commands(self) -> None:
        clear_help_cache()
        doc = {
            "commands": {
                "list": {"side_effects": "read-only"},
                "run": {"side_effects": "mutating", "usage": "widget run -- <argv...>"},
            }
        }
        mod = {
            "name": "mcp.widget",
            "kind": "mcp",
            "when": "on_request",
            "cadence": "on_request",
            "verb": "python -m widget",
            "rests_on": "fake",
            "expected_exit": 0,
            "mutates": True,
            "commands_from": "help-json",
        }

        class Proc:
            returncode = 0
            stdout = json.dumps(doc)
            stderr = ""

        real_run = subprocess.run

        def _run(argv, *args, **kwargs):
            if isinstance(argv, list) and "--help-json" in argv:
                return Proc()
            return real_run(argv, *args, **kwargs)

        env = {"AGENTS_APPROVAL_CMD": "false", "AGENTS_APPROVAL_MODE": "ask"}
        with patch.dict(os.environ, env, clear=False):
            with patch("runner.helpjson.subprocess.run", side_effect=_run):
                with patch("runner.cordis_tools.find_module", return_value=dict(mod)):
                    with patch(
                        "runner.cordis_tools.execute_job",
                        return_value={"status": "SUCCESS", "stdout": "listed", "stderr": ""},
                    ) as run:
                        out = handle_cordis_tool(
                            "call_job",
                            {
                                "function": {
                                    "arguments": json.dumps(
                                        {"name": "mcp.widget", "arguments": {"argv": ["list", "."]}}
                                    )
                                }
                            },
                        )
                    self.assertEqual(run.call_args.kwargs["extra_argv"], ["list", "."])
                    self.assertIn("listed", out)
                    with patch("runner.cordis_tools.execute_job") as blocked:
                        denied = handle_cordis_tool(
                            "call_job",
                            {
                                "function": {
                                    "arguments": json.dumps(
                                        {
                                            "name": "mcp.widget",
                                            "arguments": {"argv": ["run", "--", "rm", "x"]},
                                        }
                                    )
                                }
                            },
                        )
                    blocked.assert_not_called()
                    self.assertTrue(denied.startswith("Denied:"))
        clear_help_cache()


if __name__ == "__main__":
    unittest.main()
