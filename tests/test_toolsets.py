"""Named toolsets: config sets, env/flag/channel selection, call_job gate."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from agents_workspace import Host

from runner import toolsets
from runner.cordis_tools import run_cordis_tool


def _call(name: str, args: dict, **kw):
    return run_cordis_tool(name, {"function": {"arguments": args}}, **kw)


class TestToolsets(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.host = Host(dir=Path(self._tmp.name) / "host")
        self.ws = self.host.create("demo")
        self._saved_env = os.environ.get("AGENTS_TOOLSET")
        os.environ.pop("AGENTS_TOOLSET", None)

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("AGENTS_TOOLSET", None)
        else:
            os.environ["AGENTS_TOOLSET"] = self._saved_env

    def _config(self, doc: dict) -> None:
        self.ws.config.write_text(json.dumps(doc), encoding="utf-8")

    def test_unrestricted_when_no_config(self):
        self.assertIsNone(toolsets.allowed_patterns(workspace=self.ws))
        self.assertTrue(toolsets.module_allowed("mcp.memory.search", workspace=self.ws))

    def test_env_selection_filters(self):
        self._config({"toolsets": {"safe": ["skill.*"]}})
        os.environ["AGENTS_TOOLSET"] = "safe"
        self.assertTrue(toolsets.module_allowed("skill.list", workspace=self.ws))
        self.assertFalse(toolsets.module_allowed("mcp.terminal", workspace=self.ws))

    def test_env_all_clears(self):
        self._config({"toolsets": {"safe": ["skill.*"]}, "toolset": "safe"})
        os.environ["AGENTS_TOOLSET"] = "all"
        self.assertIsNone(toolsets.allowed_patterns(workspace=self.ws))

    def test_config_string_pick(self):
        self._config({"toolsets": {"safe": ["skill.*"]}, "toolset": "safe"})
        self.assertEqual(toolsets.allowed_patterns(workspace=self.ws), ["skill.*"])

    def test_channel_pick(self):
        self._config({
            "toolsets": {"safe": ["skill.*"]},
            "toolset": {"telegram": "safe"},
        })
        self.assertEqual(
            toolsets.allowed_patterns("telegram", self.ws), ["skill.*"])
        self.assertIsNone(toolsets.allowed_patterns("cli", self.ws))

    def test_unknown_set_fails_closed(self):
        self._config({"toolsets": {"safe": ["skill.*"]}, "toolset": "typo"})
        self.assertEqual(toolsets.allowed_patterns(workspace=self.ws), [])
        self.assertFalse(toolsets.module_allowed("skill.list", workspace=self.ws))

    def test_call_job_refuses_outside_set(self):
        self._config({"toolsets": {"safe": ["skill.*"]}})
        os.environ["AGENTS_TOOLSET"] = "safe"
        out = _call("call_job", {"name": "mcp.memory.search"}, workspace=self.ws)
        self.assertFalse(out.success)
        self.assertIn("not in active toolset", str(out.text))

    def test_call_job_allows_member(self):
        self._config({"toolsets": {"safe": ["skill.*"]}})
        os.environ["AGENTS_TOOLSET"] = "safe"
        out = _call(
            "call_job",
            {"name": "skill.list", "arguments": {}},
            workspace=self.ws,
        )
        self.assertTrue(out.success)

    def test_list_catalog_filters(self):
        os.environ["AGENTS_TOOLSET"] = "safe"
        self._config({"toolsets": {"safe": ["skill.*"]}})
        out = _call("list_catalog", {}, workspace=self.ws)
        lines = [ln for ln in str(out.text).splitlines() if ln.strip()]
        self.assertTrue(all(ln.split("\t")[0].startswith("skill.") for ln in lines))
        self.assertTrue(lines)

    def test_group_member_filters_commands(self):
        # mcp.calendar:read-only admits read-only commands, refuses mutating ones
        self._config({"toolsets": {"safe": ["mcp.calendar:read-only"]}})
        os.environ["AGENTS_TOOLSET"] = "safe"
        self.assertTrue(toolsets.module_allowed("mcp.calendar", workspace=self.ws))
        self.assertFalse(toolsets.module_allowed("mcp.browser", workspace=self.ws))
        mod = {"name": "mcp.calendar", "commands": {
            "list": {"side_effects": "read-only"},
            "add": {"side_effects": "mutating"},
        }}
        self.assertTrue(toolsets.command_allowed(mod, {"command": "list"}, workspace=self.ws))
        self.assertFalse(toolsets.command_allowed(mod, {"command": "add"}, workspace=self.ws))
        self.assertFalse(toolsets.command_allowed(mod, {}, workspace=self.ws))

    def test_explicit_groups_beat_side_effects(self):
        self._config({"toolsets": {"g": ["mcp.browser:nav"]}})
        os.environ["AGENTS_TOOLSET"] = "g"
        mod = {"name": "mcp.browser", "commands": {
            "open": {"side_effects": "mutating", "groups": ["nav"]},
            "snapshot": {"side_effects": "read-only"},
        }}
        self.assertTrue(toolsets.command_allowed(mod, {"command": "open"}, workspace=self.ws))
        # read-only is its implicit group, not "nav"
        self.assertFalse(toolsets.command_allowed(mod, {"command": "snapshot"}, workspace=self.ws))

    def test_plain_member_allows_all_commands(self):
        self._config({"toolsets": {"g": ["mcp.calendar"]}})
        os.environ["AGENTS_TOOLSET"] = "g"
        mod = {"name": "mcp.calendar", "commands": {"add": {"side_effects": "mutating"}}}
        self.assertTrue(toolsets.command_allowed(mod, {"command": "add"}, workspace=self.ws))


class TestAutoMount(unittest.TestCase):
    def test_cordis_expose_synthesizes_module(self):
        from unittest.mock import patch
        from runner import modules, helpjson

        doc = {
            "name": "agents-widget",
            "cordis_expose": True,
            "commands": {
                "ping": {"side_effects": "read-only"},
                "poke": {"side_effects": "mutating"},
            },
        }
        seen = {}
        with patch.dict(os.environ, {"AGENTS_CORDIS_PKGS": "agents_widget"}):
            with patch.object(modules, "_AUTO_PKGS", ()):
                with patch.object(helpjson, "load_help_json", return_value=doc):
                    with patch("importlib.util.find_spec", return_value=True):
                        modules._auto_mount(seen)
        self.assertIn("mcp.widget", seen)
        self.assertEqual(seen["mcp.widget"]["verb"], "python -m agents_widget")
        self.assertTrue(seen["mcp.widget"]["as_tool"])

    def test_existing_manifest_wins(self):
        from unittest.mock import patch
        from runner import modules, helpjson

        doc = {"cordis_expose": True, "commands": {"x": {}}}
        seen = {"mcp.keys": {"name": "mcp.keys", "verb": "custom"}}
        with patch.object(helpjson, "load_help_json", return_value=doc):
            with patch("importlib.util.find_spec", return_value=True):
                modules._auto_mount(seen)
        self.assertEqual(seen["mcp.keys"]["verb"], "custom")
        # the mounted package name is derived from the import, not the doc
        self.assertIn("mcp.keys", seen)

    def test_no_flag_no_mount(self):
        from unittest.mock import patch
        from runner import modules, helpjson

        seen = {}
        with patch.object(helpjson, "load_help_json", return_value={"commands": {"x": {}}}):
            with patch("importlib.util.find_spec", return_value=True):
                modules._auto_mount(seen)
        self.assertEqual(seen, {})


if __name__ == "__main__":

    unittest.main()
