"""Workspace model selection: persisted provider/model pick."""

import os
import tempfile
import unittest
from unittest.mock import patch

from agents_workspace import Host
from runner import model
from runner.providers import get_provider


class TestModelPick(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = patch.dict(
            os.environ,
            {
                "AGENTS_HOST_DIR": self._tmp.name,
                "AGENTS_WORKSPACE": "demo",
                "LLM_PROVIDER": "",
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        Host.from_env().create("demo", "demo")

    def test_default_empty_pick(self) -> None:
        self.assertEqual(model.selected(), {"provider": "", "model": ""})

    def test_use_and_clear(self) -> None:
        out = model.use("echo")
        self.assertEqual(out["provider"], "echo")
        self.assertEqual(model.selected()["provider"], "echo")
        model.clear()
        self.assertEqual(model.selected()["provider"], "")

    def test_use_unknown_provider(self) -> None:
        with self.assertRaises(model.ModelError):
            model.use("no.such.provider")

    def test_get_provider_uses_pick(self) -> None:
        model.use("echo")
        provider = get_provider()
        self.assertEqual(provider.name, "echo")

    def test_env_beats_pick(self) -> None:
        model.use("echo")
        with patch.dict(os.environ, {"LLM_PROVIDER": "openai.default"}):
            provider = get_provider()
        self.assertEqual(provider.name, "openai.default")

    def test_current_sources(self) -> None:
        model.use("echo", model="m1")
        cur = model.current()
        self.assertEqual(cur["provider"], "echo")
        self.assertEqual(cur["provider_source"], "workspace")
        self.assertEqual(cur["model"], "m1")
        self.assertEqual(cur["model_source"], "workspace")

    def test_current_precedence_workspace_beats_env_beats_default(self) -> None:
        with patch.dict(os.environ, {"LLM_PROVIDER": "", "LLM_MODEL": ""}):
            cur_default = model.current()
            self.assertEqual(cur_default["provider_source"], "default")

        with patch.dict(os.environ, {"LLM_PROVIDER": "echo", "LLM_MODEL": "env-m"}):
            cur_env = model.current()
            self.assertEqual(cur_env["provider"], "echo")
            self.assertEqual(cur_env["provider_source"], "env")

        model.use("echo", model="ws-m")
        with patch.dict(os.environ, {"LLM_PROVIDER": "openai.default", "LLM_MODEL": "env-m"}):
            cur_ws = model.current()
            self.assertEqual(cur_ws["provider"], "echo")
            self.assertEqual(cur_ws["provider_source"], "workspace")
            self.assertEqual(cur_ws["model"], "ws-m")
            self.assertEqual(cur_ws["model_source"], "workspace")

    def test_current_explicit_workspace_does_not_leak_active_pick(self) -> None:
        ws_other = Host.from_env().create("other", "other")
        model.use("echo", model="m1")
        with patch.dict(os.environ, {"LLM_PROVIDER": "", "LLM_MODEL": ""}):
            cur_other = model.current(ws_other)
            self.assertEqual(cur_other["provider_source"], "default")
            self.assertNotEqual(cur_other["model"], "m1")


class TestModelDeclaredCommands(unittest.TestCase):
    """commands_from: help-json wires the module without a cordis_tools shim."""

    def test_declared_argv(self) -> None:
        from runner.helpjson import declared_argv, with_discovered_commands
        from runner.modules import find_module

        mod = with_discovered_commands(find_module("mcp.model"))
        self.assertEqual(
            declared_argv(mod, {"command": "use", "provider": "echo"}),
            ["use", "--provider", "echo"],
        )
        self.assertEqual(
            declared_argv(mod, {"command": "use", "provider": "echo", "model": "m1"}),
            ["use", "--provider", "echo", "--model", "m1"],
        )
        self.assertEqual(declared_argv(mod, {"command": "current"}), ["current"])
        self.assertEqual(declared_argv(mod, {"command": "clear"}), ["clear"])

    def test_declared_effects(self) -> None:
        from runner.helpjson import call_effects, with_discovered_commands
        from runner.modules import find_module

        mod = with_discovered_commands(find_module("mcp.model"))
        self.assertEqual(call_effects(mod, {"command": "use"}), "mutating")
        self.assertEqual(call_effects(mod, {"command": "current"}), "read-only")


if __name__ == "__main__":
    unittest.main()
