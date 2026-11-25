"""Tests for Cordis closed-carrier tools, redaction, and argv execution."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.cordis_tools import handle_cordis_tool, openai_cordis_tools
from runner.executor import execute_job, verb_to_argv
from runner.modules import find_module, openai_tools
from runner.redact import redact_tool_output


class TestCordisTools(unittest.TestCase):
    def test_openai_tools_surface(self):
        names = {t["function"]["name"] for t in openai_tools()}
        self.assertEqual(names, {"list_catalog", "load_schema", "call_job", "clarify"})

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
        with patch.dict(os.environ, {"AGENTS_WORKSPACE_ROOT": "/data/.agents"}, clear=False):
            raw = "token sk-abcdefghijklmnopqrstuvwxyz path /data/.agents/identity.json"
            out = redact_tool_output(raw)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", out)
        self.assertNotIn("/data/.agents/identity.json", out)

    def test_redact_secret_env_patterns_and_values_without_root_warning(self):
        import warnings

        secret_keys = (
            "RELAY_SECRET",
            "GATEWAY_SECRET",
            "RELAY_WEB_TOKEN",
            "WEBHOOK_SECRET",
            "AGENTS_TERMINAL_NOTIFY_SECRET",
            "AGENTS_APPROVAL_TOKEN",
            "AGENTS_SYNC_TOKEN",
            "AGENTS_SYNC_SERVER_SECRET",
        )
        with tempfile.TemporaryDirectory() as tmp:
            from agents_workspace import Host

            host = Host(dir=Path(tmp) / "host")
            host.create("demo")
            env_vals = {key: f"val_{key.lower()}_987654" for key in secret_keys}
            env_vals["AGENTS_HOST_DIR"] = str(host.dir)
            env_vals["AGENTS_WORKSPACE"] = "demo"
            lines = [f"{k}=inline_{k.lower()}" for k in secret_keys] + list(env_vals.values())
            raw = "\n".join(lines)
            with patch.dict(os.environ, env_vals, clear=False):
                with warnings.catch_warnings(record=True) as seen:
                    warnings.simplefilter("always")
                    out = redact_tool_output(raw)
            self.assertFalse(
                any("Workspace.root is deprecated" in str(w.message) for w in seen),
                seen,
            )
            for key in secret_keys:
                self.assertNotIn(f"inline_{key.lower()}", out)
                self.assertNotIn(env_vals[key], out)
                self.assertNotIn(f"{key}=", out)
            self.assertIn("[redacted]", out)

    def test_execute_job_redacts_stdout(self):
        mod = find_module("skill.catalog")
        self.assertIsNotNone(mod)
        with patch("runner.executor.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "secret sk-1234567890123456"
            run.return_value.stderr = ""
            rec = execute_job(mod)
        self.assertNotIn("sk-1234567890123456", rec["stdout_tail"])
        self.assertNotIn("sk-1234567890123456", rec["stdout"])

    def test_call_job_under_cap_returns_full_stdout(self):
        full = "only two events"
        rec = {"status": "SUCCESS", "stdout": full, "stdout_tail": full[-1000:]}
        call = {
            "function": {
                "arguments": json.dumps(
                    {
                        "name": "mcp.calendar.list",
                        "arguments": {
                            "from": "2026-11-01T00:00:00+01:00",
                            "to": "2026-11-02T00:00:00+01:00",
                        },
                    }
                )
            }
        }
        with patch.dict(os.environ, {"AGENTS_TOOL_OUTPUT_MAX": "200"}, clear=False):
            with patch("runner.cordis_tools.execute_job", return_value=rec):
                out = handle_cordis_tool("call_job", call)
        self.assertEqual(out, full)

    def test_call_job_over_cap_keeps_head_tail_and_marker(self):
        full = "START" + ("m" * 4000) + "END"
        rec = {
            "status": "SUCCESS",
            "stdout": full,
            "stderr": "",
            "stdout_tail": full[-1000:],
            "stderr_tail": "",
        }
        call = {
            "function": {
                "arguments": json.dumps(
                    {
                        "name": "mcp.calendar.list",
                        "arguments": {
                            "from": "2026-11-01T00:00:00+01:00",
                            "to": "2026-11-02T00:00:00+01:00",
                        },
                    }
                )
            }
        }
        with patch.dict(os.environ, {"AGENTS_TOOL_OUTPUT_MAX": "180"}, clear=False):
            with patch("runner.cordis_tools.execute_job", return_value=rec):
                out = handle_cordis_tool("call_job", call)
        self.assertTrue(out.startswith("START"), out)
        self.assertNotIn("END", out.split("\n<page ", 1)[0])
        self.assertNotIn("chars omitted", out)
        self.assertIn("<page ", out)
        self.assertIn('truncated="1"', out)
        self.assertIn('next_offset="', out)
        body, ref = out.rsplit("\n<page ", 1)
        ref_line = "<page " + ref
        self.assertNotIn("\n", ref_line)
        self.assertLessEqual(len(body), 180)
        self.assertLessEqual(len(out), 180 + 1 + len(ref_line))
        self.assertEqual(len(out), len(body) + 1 + len(ref_line))
        self.assertNotEqual(out, full[-1000:])
        self.assertEqual(body, full[: len(body)])
        digest = re.search(r'digest="([0-9a-f]+)"', out)
        self.assertIsNotNone(digest)
        assert digest is not None
        fetched = handle_cordis_tool(
            "call_job",
            {
                "function": {
                    "arguments": json.dumps(
                        {"name": "read_content", "arguments": {"digest": digest.group(1), "offset": 0, "limit": -1}}
                    )
                }
            },
        )
        page = json.loads(fetched)
        self.assertEqual(page["text"], full)
        self.assertFalse(page["truncated"])
        self.assertEqual(page["total_chars"], len(full))

    def test_call_job_failure_clips_stderr(self):
        err = "ERRHEAD" + ("e" * 2000) + "ERRTAIL"
        rec = {
            "status": "FAILED",
            "stdout": "ignored-stdout",
            "stderr": err,
            "stdout_tail": "ignored-stdout",
            "stderr_tail": err[-1000:],
        }
        call = {
            "function": {
                "arguments": json.dumps({"name": "mcp.calendar.calendars", "arguments": {}})
            }
        }
        with patch.dict(os.environ, {"AGENTS_TOOL_OUTPUT_MAX": "160"}, clear=False):
            with patch("runner.cordis_tools.execute_job", return_value=rec):
                out = handle_cordis_tool("call_job", call)
        self.assertTrue(out.startswith("ERRHEAD"), out)
        self.assertNotIn("ERRTAIL", out.split("\n<page ", 1)[0])
        self.assertNotIn("chars omitted", out)
        self.assertIn("<page ", out)
        self.assertNotIn("ignored-stdout", out)
        body, ref = out.rsplit("\n<page ", 1)
        ref_line = "<page " + ref
        self.assertNotIn("\n", ref_line)
        self.assertLessEqual(len(body), 160)
        self.assertLessEqual(len(out), 160 + 1 + len(ref_line))
        self.assertEqual(len(out), len(body) + 1 + len(ref_line))

    def test_enabled_json_can_disable_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp)
            (overlay / "enabled.json").write_text("[]", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_MODULES_DIR": str(overlay)}):
                self.assertEqual(openai_tools(), [])

    def test_openai_cordis_tools_schema(self):
        tools = openai_cordis_tools()
        self.assertEqual(len(tools), 4)

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


class TestValidateJobArgs(unittest.TestCase):
    """Fail-loud argument validation: invented/missing keys must come back
    with the valid names in the same turn, never as a deep tool error."""

    def _tau2_mod(self):
        props = {
            "user_id": {"type": "string"},
            "cabin": {"type": "string"},
            "flights": {"type": "array"},
        }
        required = ["user_id", "cabin", "flights"]
        # tau2 shape: spec drops required for argv building; the module level
        # keeps the full original schema with the same property set.
        spec_params = {"type": "object", "properties": dict(props)}
        return {
            "name": "tau2.book_reservation",
            "parameters": {"type": "object", "properties": dict(props), "required": required},
            "command_pin": "call",
            "commands": {"call": {"action": "tau2.x", "parameters": spec_params}},
        }

    def test_invented_keys_fail_loud_with_valid_names(self):
        from runner.cordis_tools import _validate_job_args

        err = _validate_job_args(self._tau2_mod(), {"user_id": "u", "cabin_class": "economy", "flights": []})
        self.assertIn("missing required: cabin", err)
        self.assertIn("unknown: cabin_class", err)
        self.assertIn("valid parameters: user_id, cabin, flights", err)

    def test_schema_correct_call_passes(self):
        from runner.cordis_tools import _validate_job_args

        err = _validate_job_args(
            self._tau2_mod(),
            {"command": "call", "user_id": "u", "cabin": "economy", "flights": []},
        )
        self.assertEqual(err, "")

    def test_command_routed_envelope_never_validates_direct_args(self):
        """Router-envelope modules (mcp.browser shape) must not reject direct
        argument dicts: their top-level schema documents command/argv routing,
        not a callable contract."""
        from runner.cordis_tools import _validate_job_args

        mod = {
            "name": "mcp.browser",
            "commands_from": "help-json",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}, "argv": {"type": "array"}},
            },
        }
        self.assertEqual(_validate_job_args(mod, {"url": "https://x.test"}), "")

    def test_open_world_schema_allows_extras(self):
        from runner.cordis_tools import _validate_job_args

        mod = {
            "name": "mcp.flex",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "additionalProperties": True,
            },
        }
        self.assertEqual(_validate_job_args(mod, {"query": "q", "extra": 1}), "")

    def test_tolerant_mapper_modules_exempt_from_unknown_rejection(self):
        from runner.cordis_tools import _validate_job_args

        mod = {
            "name": "mcp.memory.read",
            "parameters": {"type": "object", "properties": {"file_id": {"type": "string"}}},
        }
        self.assertEqual(_validate_job_args(mod, {"path": "USER.md"}), "")

    def test_module_without_schema_passes(self):
        from runner.cordis_tools import _validate_job_args

        self.assertEqual(_validate_job_args({"name": "x"}, {"anything": 1}), "")

    def test_no_required_borrow_when_property_sets_differ(self):
        """An envelope-shaped top level must not lend its required list down
        into a command spec with a different contract."""
        from runner.cordis_tools import _validate_job_args

        mod = {
            "name": "mcp.mixed",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            "command_pin": "go",
            "commands": {
                "go": {"action": "x", "parameters": {"type": "object", "properties": {"speed": {"type": "integer"}}}}
            },
        }
        # 'command' is not part of the spec contract; nothing is missing.
        self.assertEqual(_validate_job_args(mod, {"speed": 2}), "")

    def test_raw_argv_escape_hatch_skips_missing_required(self):
        from runner.cordis_tools import _validate_job_args

        mod = {
            "name": "mcp.terminal",
            "commands": {
                "write": {
                    "action": "fs.write",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path"],
                    },
                }
            },
        }
        self.assertEqual(_validate_job_args(mod, {"argv": ["write", "a.txt", "--content", "hi"]}), "")
        self.assertIn("missing required: path", _validate_job_args(mod, {"command": "write", "argv": []}))
        self.assertIn("missing required: path", _validate_job_args(mod, {"command": "write", "content": "hi"}))

    def test_spec_declaring_argv_still_validates_required_argv(self):
        from runner.cordis_tools import _validate_job_args

        mod = {
            "name": "mcp.terminal",
            "commands": {
                "run": {
                    "action": "proc.run",
                    "parameters": {
                        "type": "object",
                        "properties": {"argv": {"type": "array"}},
                        "required": ["argv"],
                    },
                }
            },
        }
        self.assertIn("missing required: argv", _validate_job_args(mod, {"command": "run"}))

    def test_raw_argv_non_tolerant_still_rejects_unknown_keys(self):
        from runner.cordis_tools import _validate_job_args

        mod = {
            "name": "mcp.stricttool",
            "commands": {
                "go": {
                    "action": "x.go",
                    "parameters": {
                        "type": "object",
                        "properties": {"target": {"type": "string"}},
                        "required": ["target"],
                    },
                }
            },
        }
        self.assertIn("unknown: bogus", _validate_job_args(mod, {"argv": ["go", "x"], "bogus": 1}))

    def test_call_job_description_carries_discovery_contract(self):
        desc = ""
        for tool in openai_cordis_tools():
            if tool["function"]["name"] == "call_job":
                desc = tool["function"]["description"]
        self.assertIn("load_schema", desc)
        self.assertIn("rejected", desc)

    def test_calendar_argv_wall_time_timezone_and_all_day(self):
        from runner.cordis_tools import _arguments_to_argv

        timed = _arguments_to_argv(
            {"name": "mcp.calendar.add"},
            {
                "summary": "Standup",
                "dtstart": "2026-11-29T16:00:00",
                "dtend": "2026-11-29T17:00:00",
                "timezone": "Europe/Berlin",
                "all_day": False,
                "calendar": "Work",
            },
        )
        self.assertEqual(
            timed,
            [
                "--summary",
                "Standup",
                "--dtstart",
                "2026-11-29T16:00:00",
                "--dtend",
                "2026-11-29T17:00:00",
                "--calendar",
                "Work",
                "--timezone",
                "Europe/Berlin",
            ],
        )
        allday = _arguments_to_argv(
            {"name": "mcp.calendar.add"},
            {
                "summary": "Off",
                "dtstart": "2026-11-29",
                "dtend": "2026-11-30",
                "all_day": True,
            },
        )
        self.assertEqual(
            allday,
            [
                "--summary",
                "Off",
                "--dtstart",
                "2026-11-29",
                "--dtend",
                "2026-11-30",
                "--all-day",
            ],
        )
        self.assertEqual(
            _arguments_to_argv(
                {"name": "mcp.calendar.add"},
                {"summary": "Off", "dtstart": "2026-11-29", "dtend": "2026-11-30", "all_day": "true"},
            )[-1],
            "--all-day",
        )
        self.assertNotIn(
            "--all-day",
            _arguments_to_argv(
                {"name": "mcp.calendar.add"},
                {"summary": "Off", "dtstart": "2026-11-29", "dtend": "2026-11-30", "all_day": "false"},
            ),
        )
        listed = _arguments_to_argv(
            {"name": "mcp.calendar.list"},
            {
                "from": "2026-11-29T00:00:00",
                "to": "2026-11-30T00:00:00",
                "calendar": "Work",
                "timezone": "Europe/Berlin",
            },
        )
        self.assertEqual(
            listed,
            [
                "--from",
                "2026-11-29T00:00:00",
                "--to",
                "2026-11-30T00:00:00",
                "--calendar",
                "Work",
                "--timezone",
                "Europe/Berlin",
            ],
        )
        updated = _arguments_to_argv(
            {"name": "mcp.calendar.update"},
            {
                "href": "/ev",
                "etag": "x",
                "dtstart": "2026-11-29",
                "timezone": "Europe/Berlin",
                "all_day": True,
            },
        )
        self.assertEqual(
            updated,
            ["--href", "/ev", "--etag", "x", "--dtstart", "2026-11-29", "--timezone", "Europe/Berlin"],
        )
        self.assertNotIn("--all-day", updated)

    def test_calendar_manifests_describe_wall_time(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENTS_MODULES_DIR"}
        with patch.dict(os.environ, env, clear=True):
            add = find_module("mcp.calendar.add")
            update = find_module("mcp.calendar.update")
            listing = find_module("mcp.calendar.list")
        self.assertIsNotNone(add)
        self.assertIsNotNone(update)
        self.assertIsNotNone(listing)
        assert add is not None and update is not None and listing is not None
        for mod in (add, update, listing):
            blob = json.dumps(mod)
            self.assertNotIn("treated as UTC", blob)
            self.assertNotIn("with a UTC offset", blob)
            self.assertIn("timezone", mod["parameters"]["properties"])
            tz = mod["parameters"]["properties"]["timezone"]["description"]
            self.assertIn("CALDAV_TIMEZONE", tz)
            self.assertIn("calendar-timezone", tz)
            self.assertIn("host zone", tz)
            self.assertIn("UTC", tz)
            time_key = "dtstart" if "dtstart" in mod["parameters"]["properties"] else "from"
            self.assertIn("wall time", mod["parameters"]["properties"][time_key]["description"])
        self.assertIn("all_day", add["parameters"]["properties"])
        self.assertNotIn("all_day", update["parameters"]["properties"])
        listed = json.dumps(listing["parameters"])
        self.assertIn("dtstart_local", listed)
        self.assertIn("all_day", listed)
        self.assertIn("explicit offset", add["parameters"]["properties"]["dtstart"]["description"])
        self.assertIn("ambiguous", add["parameters"]["properties"]["dtstart"]["description"])
        self.assertIn("gap", listing["parameters"]["properties"]["from"]["description"])

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
