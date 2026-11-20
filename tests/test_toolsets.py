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


if __name__ == "__main__":
    unittest.main()
