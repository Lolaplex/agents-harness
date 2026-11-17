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


if __name__ == "__main__":
    unittest.main()
