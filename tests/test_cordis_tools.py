"""Tests for Cordis closed-carrier tools, redaction, and argv execution."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.cordis_tools import handle_cordis_tool, openai_cordis_tools
from runner.executor import execute_job, verb_to_argv
from runner.modules import find_module, openai_tools
from runner.redact import redact_tool_output


class TestCordisTools(unittest.TestCase):
    def test_openai_tools_are_three(self):
        names = {t["function"]["name"] for t in openai_tools()}
        self.assertEqual(names, {"list_catalog", "load_schema", "call_job"})

    def test_list_catalog_includes_terminal(self):
        out = handle_cordis_tool("list_catalog", {"function": {"arguments": "{}"}})
        self.assertIn("mcp.terminal", out)

    def test_call_job_routes_list_catalog(self):
        out = handle_cordis_tool(
            "call_job",
            {
                "function": {
                    "arguments": json.dumps({"name": "list_catalog"}),
                }
            },
        )
        self.assertIn("mcp.terminal", out)

    def test_unknown_call_job_refused(self):
        out = handle_cordis_tool(
            "call_job",
            {
                "function": {
                    "arguments": json.dumps({"name": "mcp.identity.mint"}),
                }
            },
        )
        self.assertIn("refused", out.lower())

    def test_invented_tool_name_refused_in_loop_path(self):
        from runner.loop import _run_tool_calls

        msgs = _run_tool_calls(
            [
                {
                    "id": "x1",
                    "type": "function",
                    "function": {"name": "mcp_memory_search", "arguments": "{}"},
                }
            ]
        )
        self.assertIn("refused", msgs[0]["content"].lower())

    def test_verb_to_argv_never_shell(self):
        argv = verb_to_argv("python -m agents_docs --help-json")
        import sys

        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1:3], ["-m", "agents_docs"])

    def test_redact_sk_and_agents_paths(self):
        with patch.dict(os.environ, {"AGENTS_HOME": "/data/.agents"}, clear=False):
            raw = "token sk-abcdefghijklmnopqrstuvwxyz path /data/.agents/identity.json"
            out = redact_tool_output(raw)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", out)
        self.assertNotIn("/data/.agents/identity.json", out)

    def test_execute_job_redacts_stdout(self):
        mod = find_module("skill.catalog")
        self.assertIsNotNone(mod)
        with patch("runner.executor.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "secret sk-1234567890123456"
            run.return_value.stderr = ""
            rec = execute_job(mod)
        self.assertNotIn("sk-1234567890123456", rec["stdout_tail"])

    def test_enabled_json_can_disable_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp)
            (overlay / "enabled.json").write_text("[]", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_MODULES_DIR": str(overlay)}):
                self.assertEqual(openai_tools(), [])

    def test_openai_cordis_tools_schema(self):
        tools = openai_cordis_tools()
        self.assertEqual(len(tools), 3)

    def test_arguments_to_argv_memory_add_strips_duplicate_add(self):
        from runner.cordis_tools import _arguments_to_argv

        mod = {"name": "mcp.memory.add", "verb": "python -m agents_memory add"}
        argv = _arguments_to_argv(mod, {"argv": ["add", "New fact content", "--kind", "fact"]})
        self.assertEqual(argv, ["New fact content", "--kind", "fact"])

    def test_arguments_to_argv_memory_add_text_alias(self):
        from runner.cordis_tools import _arguments_to_argv

        mod = {"name": "mcp.memory.add", "verb": "python -m agents_memory add"}
        argv = _arguments_to_argv(mod, {"text": "Birthday missing from list", "kind": "fact"})
        self.assertEqual(argv, ["Birthday missing from list", "--kind", "fact"])

    def test_arguments_to_argv_terminal_wraps_shell_vars(self):
        from runner.cordis_tools import _arguments_to_argv
        import sys

        mod = {"name": "mcp.terminal", "verb": "python -m agents_terminal"}
        argv = _arguments_to_argv(mod, {"command": "echo $GITHUB_TOKEN"})
        self.assertEqual(argv[0:2], ["run", "--"])
        if sys.platform == "win32":
            self.assertIn("powershell", argv[2])
        else:
            self.assertEqual(argv[2:4], ["sh", "-c"])

    def test_arguments_to_argv_docs_write_positionals(self):
        from runner.cordis_tools import _arguments_to_argv

        mod = {
            "name": "mcp.docs.write",
            "verb": "python -m agents_docs write",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                    "category": {"type": "string"},
                },
            },
        }
    def test_arguments_to_argv_memory_read_aliases(self):
        from runner.cordis_tools import _arguments_to_argv

        mod = {
            "name": "mcp.memory.read",
            "verb": "python -m agents_memory read",
            "parameters": {
                "type": "object",
                "properties": {"file_id": {"type": "string"}},
            },
        }
        self.assertEqual(_arguments_to_argv(mod, {"file": "USER.md"}), ["USER.md"])
        self.assertEqual(_arguments_to_argv(mod, {"path": "PROJECTS.md"}), ["PROJECTS.md"])
        self.assertEqual(_arguments_to_argv(mod, ["USER.md"]), ["USER.md"])
        self.assertEqual(_arguments_to_argv(mod, {"arguments": ["USER.md"]}), ["USER.md"])
        self.assertEqual(_arguments_to_argv(mod, {"arguments": "USER.md"}), ["USER.md"])

    def test_schedule_add_inherits_turn_channel_and_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENTS_SCHEDULES_DIR": tmp}):
                out = handle_cordis_tool(
                    "call_job",
                    {
                        "function": {
                            "arguments": json.dumps(
                                {
                                    "name": "mcp.schedule.add",
                                    "arguments": {
                                        "name": "pingjob",
                                        "text": "ping",
                                        "at": "+10m",
                                    },
                                }
                            )
                        }
                    },
                    default_user="42",
                    default_channel="telegram",
                )
            self.assertNotIn("Error", out)
            data = json.loads((Path(tmp) / "pingjob.json").read_text(encoding="utf-8"))
            self.assertEqual(data["channel"], "telegram")
            self.assertEqual(data["user"], "42")
            self.assertIn("--channel", data["verb"])
            self.assertIn("telegram", data["verb"])

            with patch.dict(os.environ, {"AGENTS_SCHEDULES_DIR": tmp}):
                handle_cordis_tool(
                    "call_job",
                    {
                        "function": {
                            "arguments": json.dumps(
                                {
                                    "name": "mcp.schedule.add",
                                    "arguments": {
                                        "name": "explicit",
                                        "prompt": "check",
                                        "cron": "0 8 * * *",
                                        "channel": "http",
                                    },
                                }
                            )
                        }
                    },
                    default_user="42",
                    default_channel="telegram",
                )
            explicit = json.loads((Path(tmp) / "explicit.json").read_text(encoding="utf-8"))
            self.assertEqual(explicit["channel"], "http")
            self.assertEqual(explicit["user"], "42")


if __name__ == "__main__":
    unittest.main()

