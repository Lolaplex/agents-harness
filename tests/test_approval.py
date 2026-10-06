"""Approval gate: command, mode, mutator classification, denial text."""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.approval import gate_module, module_is_mutator, module_is_read_only
from runner.cordis_tools import handle_cordis_tool
from runner.modules import find_module


class TestApproval(unittest.TestCase):
    def test_read_and_mutate_flags(self):
        search = find_module("mcp.memory.search")
        add = find_module("mcp.memory.add")
        self.assertIsNotNone(search)
        self.assertIsNotNone(add)
        self.assertTrue(module_is_read_only(search))
        self.assertFalse(module_is_mutator(search))
        self.assertTrue(module_is_mutator(add))
        remote = {
            "name": "mcp.demo.write",
            "kind": "mcp_remote",
            "readOnlyHint": False,
        }
        self.assertTrue(module_is_mutator(remote))
        hinted = {"name": "mcp.demo.search", "kind": "mcp_remote", "readOnlyHint": True}
        self.assertFalse(module_is_mutator(hinted))
        self.assertTrue(module_is_read_only(hinted))

    def test_no_command_keeps_current_behavior(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("AGENTS_APPROVAL_CMD", "AGENTS_APPROVAL_MODE")
        }
        env["AGENTS_APPROVAL_MODE"] = "strict"
        with patch.dict(os.environ, env, clear=True):
            with patch("runner.cordis_tools.execute_job", return_value={"status": "SUCCESS", "stdout_tail": "ok"}) as run:
                out = handle_cordis_tool(
                    "call_job",
                    {"function": {"arguments": json.dumps({"name": "mcp.memory.add", "arguments": {"fact": "x"}})}},
                )
        run.assert_called_once()
        self.assertIn("ok", out)

    def test_denied_does_not_run_and_says_do_not_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "approve.py"
            captured = Path(tmp) / "req.json"
            script.write_text(
                "import json,sys\n"
                "data=json.load(sys.stdin)\n"
                f"open({str(captured)!r},'w',encoding='utf-8').write(json.dumps(data))\n"
                "sys.stdout.write(json.dumps({'note':'nope'}))\n"
                "sys.exit(int(sys.argv[1]))\n",
                encoding="utf-8",
            )
            cmd = " ".join(
                [
                    shlex.quote(sys.executable),
                    shlex.quote(str(script)),
                    "1",
                    "--user",
                    "{user}",
                ]
            )
            env = {
                "AGENTS_APPROVAL_CMD": cmd,
                "AGENTS_APPROVAL_MODE": "ask",
            }
            with patch.dict(os.environ, env, clear=False):
                with patch("runner.cordis_tools.execute_job") as run:
                    out = handle_cordis_tool(
                        "call_job",
                        {
                            "function": {
                                "arguments": json.dumps(
                                    {"name": "mcp.memory.add", "arguments": {"fact": "secret fact"}}
                                )
                            }
                        },
                        default_user="5712",
                        session="ses_test",
                    )
            run.assert_not_called()
            self.assertTrue(out.startswith("Denied:"))
            self.assertIn("declined", out)
            self.assertIn("Do not retry the same tool call.", out)
            self.assertIn("Note: nope", out)
            payload = json.loads(captured.read_text(encoding="utf-8"))
            self.assertEqual(payload["user"], "5712")
            self.assertEqual(payload["session"], "ses_test")
            self.assertEqual(payload["tool"], "mcp.memory.add")
            self.assertIn("fact", payload["args"])

    def test_exit_two_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "approve.py"
            script.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
            cmd = " ".join([shlex.quote(sys.executable), shlex.quote(str(script))])
            with patch.dict(
                os.environ,
                {"AGENTS_APPROVAL_CMD": cmd, "AGENTS_APPROVAL_MODE": "ask"},
                clear=False,
            ):
                allowed, msg = gate_module(
                    {"name": "mcp.terminal", "mutates": True},
                    {"command": "ls"},
                    user="9",
                )
        self.assertFalse(allowed)
        self.assertIn("unavailable", msg)
        self.assertIn("Do not retry", msg)

    def test_ask_does_not_gate_schedule_add_strict_does(self):
        from runner.modules import find_module

        add = find_module("mcp.schedule.add")
        remove = find_module("mcp.schedule.remove")
        self.assertIsNotNone(add)
        self.assertTrue(add["mutates"])
        self.assertIs(add.get("approval_ask"), False)
        env_ask = {"AGENTS_APPROVAL_CMD": "false", "AGENTS_APPROVAL_MODE": "ask"}
        with patch.dict(os.environ, env_ask, clear=False):
            with patch("runner.cordis_tools.execute_job", return_value={"status": "SUCCESS", "stdout_tail": "added"}) as run:
                out = handle_cordis_tool(
                    "call_job",
                    {
                        "function": {
                            "arguments": json.dumps(
                                {"name": "mcp.schedule.add", "arguments": {"text": "hi", "at": "+10m", "channel": "http", "user": "1"}}
                            )
                        }
                    },
                )
            run.assert_called_once()
            self.assertIn("added", out)
            with patch("runner.cordis_tools.execute_job") as remove_run:
                denied = handle_cordis_tool(
                    "call_job",
                    {"function": {"arguments": json.dumps({"name": "mcp.schedule.remove", "arguments": {"name": "x"}})}},
                )
            remove_run.assert_not_called()
            self.assertTrue(denied.startswith("Denied:"))
        self.assertTrue(remove["mutates"])
        with patch.dict(os.environ, {"AGENTS_APPROVAL_CMD": "false", "AGENTS_APPROVAL_MODE": "strict"}, clear=False):
            with patch("runner.cordis_tools.execute_job") as strict_run:
                strict = handle_cordis_tool(
                    "call_job",
                    {
                        "function": {
                            "arguments": json.dumps(
                                {"name": "mcp.schedule.add", "arguments": {"text": "hi", "at": "+10m"}}
                            )
                        }
                    },
                )
            strict_run.assert_not_called()
            self.assertTrue(strict.startswith("Denied:"))

    def test_ask_skips_read_only(self):
        with patch.dict(
            os.environ,
            {"AGENTS_APPROVAL_CMD": "false", "AGENTS_APPROVAL_MODE": "ask"},
            clear=False,
        ):
            with patch("runner.cordis_tools.execute_job", return_value={"status": "SUCCESS", "stdout_tail": "listed"}) as run:
                out = handle_cordis_tool(
                    "call_job",
                    {"function": {"arguments": json.dumps({"name": "mcp.schedule.list"})}},
                )
        run.assert_called_once()
        self.assertIn("listed", out)

    def test_off_mode_skips_even_with_command(self):
        with patch.dict(
            os.environ,
            {"AGENTS_APPROVAL_CMD": "false", "AGENTS_APPROVAL_MODE": "off"},
            clear=False,
        ):
            with patch("runner.cordis_tools.execute_job", return_value={"status": "SUCCESS", "stdout_tail": "added"}) as run:
                handle_cordis_tool(
                    "call_job",
                    {"function": {"arguments": json.dumps({"name": "mcp.memory.add", "arguments": {"fact": "x"}})}},
                )
        run.assert_called_once()

    def test_user_token_is_substituted(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "approve.py"
            seen = Path(tmp) / "argv.txt"
            script.write_text(
                "import sys\n"
                f"open({str(seen)!r},'w',encoding='utf-8').write(sys.argv[-1])\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            cmd = " ".join(
                [shlex.quote(sys.executable), shlex.quote(str(script)), "--user", "{user}"]
            )
            with patch.dict(os.environ, {"AGENTS_APPROVAL_CMD": cmd, "AGENTS_APPROVAL_MODE": "ask"}):
                allowed, msg = gate_module(
                    {"name": "mcp.docs.write", "mutates": True},
                    {"name": "doc"},
                    user="chat 42",
                )
            self.assertTrue(allowed, msg)
            self.assertEqual(seen.read_text(encoding="utf-8"), "chat 42")


if __name__ == "__main__":
    unittest.main()
