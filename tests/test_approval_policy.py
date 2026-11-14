"""Per-action rules tighten the approval tiers and do not loosen them."""

from __future__ import annotations

import json
import os
import shlex
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agents_workspace import Host

from runner.approval import gate_module, needs_approval
from runner.approval_policy import (
    PolicyError,
    action_keys,
    clear_action_classifiers,
    load_rules,
    register_action_classifier,
    rule_matches,
)
from runner.cordis_tools import handle_cordis_tool
from runner.modules import find_module


def _env(**extra: str) -> dict[str, str]:
    drop = {
        "AGENTS_APPROVAL_CMD",
        "AGENTS_APPROVAL_MODE",
        "AGENTS_APPROVAL_RULES",
        "AGENTS_APPROVAL_POLICY",
        "AGENTS_HOST_DIR",
        "AGENTS_WORKSPACE",
    }
    env = {key: value for key, value in os.environ.items() if key not in drop}
    env.update(extra)
    return env


class ActionKeys(unittest.TestCase):
    def tearDown(self):
        clear_action_classifiers()

    def test_fs_delete_comes_from_command_metadata(self):
        mod = {
            "name": "files",
            "mutates": True,
            "commands": {
                "delete": {"side_effects": "mutating", "action": "fs.delete"},
            },
        }
        keys = action_keys(mod, {"command": "delete", "path": "notes.txt"})
        self.assertIn("fs.delete", keys)
        self.assertIn("cmd:delete", keys)
        self.assertIn("tool:files", keys)

    def test_classifier_can_add_a_key(self):
        register_action_classifier(lambda _mod, _args: ["fs.delete"])
        keys = action_keys({"name": "mcp.memory.search", "mutates": False}, {})
        self.assertIn("fs.delete", keys)

    def test_push_pattern_matches_a_bare_push(self):
        self.assertTrue(rule_matches("git.push:*", "git.push:topic"))
        self.assertTrue(rule_matches("git.push:*", "git.push"))
        self.assertFalse(rule_matches("git.push:release", "git.push:topic"))


class RulesDoNotWeaken(unittest.TestCase):
    def tearDown(self):
        clear_action_classifiers()

    def test_no_rules_keeps_ask_skip_for_read_only(self):
        git_mod = find_module("git")
        self.assertIsNotNone(git_mod)
        with patch.dict(
            os.environ,
            _env(AGENTS_APPROVAL_CMD="false", AGENTS_APPROVAL_MODE="ask"),
            clear=True,
        ):
            self.assertFalse(needs_approval(git_mod, {"command": "status"}))
            allowed, msg = gate_module(git_mod, {"command": "status"})
        self.assertTrue(allowed)
        self.assertEqual(msg, "")

    def test_ask_rule_prompts_a_read_only_command(self):
        git_mod = find_module("git")
        with TemporaryDirectory() as tmp:
            seen = Path(tmp) / "seen"
            script = Path(tmp) / "ok.py"
            script.write_text(
                "import sys\n"
                f"open({str(seen)!r},'w').write('yes')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            cmd = " ".join([shlex.quote(os.sys.executable), shlex.quote(str(script))])
            rules = json.dumps([{"action": "git.status", "decision": "ask"}])
            with patch.dict(
                os.environ,
                _env(AGENTS_APPROVAL_CMD=cmd, AGENTS_APPROVAL_MODE="ask", AGENTS_APPROVAL_RULES=rules),
                clear=True,
            ):
                allowed, msg = gate_module(git_mod, {"command": "status"})
            self.assertTrue(allowed, msg)
            self.assertEqual(seen.read_text(encoding="utf-8"), "yes")

    def test_allow_rule_still_asks_a_mutator(self):
        add = find_module("mcp.memory.add")
        with TemporaryDirectory() as tmp:
            seen = Path(tmp) / "seen"
            script = Path(tmp) / "ok.py"
            script.write_text(
                "import sys\n"
                f"open({str(seen)!r},'w').write('yes')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            cmd = " ".join([shlex.quote(os.sys.executable), shlex.quote(str(script))])
            rules = json.dumps([{"action": "tool:mcp.memory.add", "decision": "allow"}])
            with patch.dict(
                os.environ,
                _env(AGENTS_APPROVAL_CMD=cmd, AGENTS_APPROVAL_MODE="ask", AGENTS_APPROVAL_RULES=rules),
                clear=True,
            ):
                allowed, msg = gate_module(add, {"fact": "x"})
            self.assertTrue(allowed, msg)
            self.assertTrue(seen.is_file())

    def test_deny_blocks_without_a_command_and_does_not_run(self):
        mod = {
            "name": "files",
            "verb": "python -c \"print(1)\"",
            "cadence": "on_request",
            "rests_on": "test",
            "mutates": True,
            "commands": {"delete": {"side_effects": "mutating", "action": "fs.delete"}},
        }
        rules = json.dumps([{"action": "fs.delete", "decision": "deny"}])
        with patch.dict(
            os.environ,
            _env(AGENTS_APPROVAL_MODE="off", AGENTS_APPROVAL_RULES=rules),
            clear=True,
        ):
            allowed, msg = gate_module(mod, {"command": "delete", "path": "a"})
            with patch("runner.cordis_tools.execute_job") as run:
                denied = handle_cordis_tool(
                    "call_job",
                    {
                        "function": {
                            "arguments": json.dumps(
                                {
                                    "name": "mcp.memory.add",
                                    "arguments": {"fact": "x", "action": "fs.delete"},
                                }
                            )
                        }
                    },
                )
        self.assertFalse(allowed)
        self.assertTrue(msg.startswith("Denied:"))
        self.assertIn("fs.delete", msg)
        self.assertIn("Do not retry", msg)
        run.assert_not_called()
        self.assertTrue(denied.startswith("Denied:"))

    def test_ask_rule_without_a_command_does_not_change_the_default(self):
        add = find_module("mcp.memory.add")
        rules = json.dumps([{"action": "tool:mcp.memory.add", "decision": "ask"}])
        with patch.dict(os.environ, _env(AGENTS_APPROVAL_RULES=rules), clear=True):
            allowed, msg = gate_module(add, {"fact": "x"})
        self.assertTrue(allowed)
        self.assertEqual(msg, "")

    def test_off_mode_with_a_command_still_honors_ask_rules(self):
        git_mod = find_module("git")
        rules = json.dumps([{"action": "git.push:*", "decision": "ask"}])
        with patch.dict(
            os.environ,
            _env(
                AGENTS_APPROVAL_CMD="false",
                AGENTS_APPROVAL_MODE="off",
                AGENTS_APPROVAL_RULES=rules,
            ),
            clear=True,
        ):
            allowed, msg = gate_module(
                git_mod, {"command": "push", "branch": "topic"}
            )
        self.assertFalse(allowed)
        self.assertIn("declined", msg)

    def test_specific_branch_deny_leaves_other_branches(self):
        git_mod = find_module("git")
        rules = json.dumps([{"action": "git.push:release", "decision": "deny"}])
        with patch.dict(os.environ, _env(AGENTS_APPROVAL_RULES=rules), clear=True):
            denied, why = gate_module(git_mod, {"command": "push", "branch": "release"})
            allowed, msg = gate_module(git_mod, {"command": "push", "branch": "topic"})
        self.assertFalse(denied)
        self.assertIn("git.push:release", why)
        self.assertTrue(allowed)
        self.assertEqual(msg, "")

    def test_invalid_rules_fail_closed(self):
        add = find_module("mcp.memory.add")
        with patch.dict(
            os.environ,
            _env(AGENTS_APPROVAL_RULES="{"),
            clear=True,
        ):
            allowed, msg = gate_module(add, {"fact": "x"})
        self.assertFalse(allowed)
        self.assertIn("not valid JSON", msg)

    def test_workspace_allow_does_not_cancel_a_host_deny(self):
        with TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            host.dir.mkdir()
            host.agents_json.write_text(
                json.dumps(
                    {"approval": {"rules": [{"action": "fs.delete", "decision": "deny"}]}}
                ),
                encoding="utf-8",
            )
            ws = host.create("demo")
            ws.config.write_text(
                json.dumps(
                    {"approval": {"rules": [{"action": "fs.delete", "decision": "allow"}]}}
                ),
                encoding="utf-8",
            )
            mod = {
                "name": "files",
                "commands": {"delete": {"side_effects": "mutating", "action": "fs.delete"}},
            }
            with patch.dict(os.environ, _env(), clear=True):
                rules = load_rules(ws)
                allowed, msg = gate_module(mod, {"command": "delete"}, workspace=ws)
        self.assertGreaterEqual(len(rules), 2)
        self.assertFalse(allowed)
        self.assertIn("fs.delete", msg)

    def test_policy_error_type_is_raised_for_a_bad_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text("{", encoding="utf-8")
            with patch.dict(os.environ, _env(AGENTS_APPROVAL_POLICY=str(path)), clear=True):
                with self.assertRaises(PolicyError):
                    load_rules()
