"""Persona memory split: persona.json memory=private|shared."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_workspace import Host

from runner import memory_rules, personas


class TestPersonaMemory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.host = Host(dir=Path(self._tmp.name) / "host")
        self.ws = self.host.create("ws1")

    def _persona(self, pid, memory=None):
        d = self.ws.personas / pid
        d.mkdir(parents=True, exist_ok=True)
        row = {"id": pid}
        if memory is not None:
            row["memory"] = memory
        (d / "persona.json").write_text(json.dumps(row), encoding="utf-8")

    def test_default_is_shared(self):
        self._persona("cos")
        self.assertEqual(personas.memory_mode("cos", self.ws), "shared")
        env = {}
        personas.apply_memory_env("cos", env, workspace=self.ws)
        self.assertEqual(env, {})

    def test_private_root_and_env(self):
        self._persona("cos", "private")
        self.assertEqual(personas.memory_mode("cos", self.ws), "private")
        env = {}
        personas.apply_memory_env("cos", env, workspace=self.ws)
        root = self.ws.personas / "cos" / "memory"
        self.assertEqual(env["AGENTS_MEMORY_PATH"], str(root))
        self.assertEqual(env["AGENTS_MEMORY_DIR"], str(root))
        self.assertTrue(root.is_dir())

    def test_private_case_insensitive_and_unknown_persona(self):
        self._persona("cos", "PRIVATE")
        self.assertEqual(personas.memory_mode("cos", self.ws), "private")
        self.assertEqual(personas.memory_mode("ghost", self.ws), "shared")

    def test_private_root_from_env_persona(self):
        self._persona("cos", "private")
        self._persona("ops")
        with patch.dict(os.environ, {"AGENTS_PERSONA": "cos"}):
            root = personas.private_memory_root_from_env(self.ws)
            self.assertEqual(root, self.ws.personas / "cos" / "memory")
        with patch.dict(os.environ, {"AGENTS_PERSONA": "ops"}):
            self.assertIsNone(personas.private_memory_root_from_env(self.ws))
        env = {k: v for k, v in os.environ.items() if k != "AGENTS_PERSONA"}
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(personas.private_memory_root_from_env(self.ws))

    def test_merged_rules_block(self):
        self._persona("cos", "private")
        memory_rules.clear_cache()
        shared = "<memory_rules>\n- shared-rule\n</memory_rules>"
        private = "<memory_rules>\n- private-rule\n</memory_rules>"
        calls = []

        def fake_cli(project, workspace=None, extra_env=None):
            calls.append(extra_env)
            return private if extra_env else shared

        env = {
            "AGENTS_PERSONA": "cos",
            "AGENTS_HOST_DIR": str(self.host.dir),
            "AGENTS_WORKSPACE": "ws1",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch.object(memory_rules, "_via_import", return_value=shared):
                with patch.object(memory_rules, "_via_cli", side_effect=fake_cli):
                    block = memory_rules.memory_rules_prompt_block("p", workspace=self.ws)
        self.assertIn("shared-rule", block)
        self.assertIn("private-rule", block)
        self.assertIn("private memory", block)
        self.assertIsNone(calls[0])
        self.assertEqual(calls[1]["AGENTS_MEMORY_PATH"], str(self.ws.personas / "cos" / "memory"))
        memory_rules.clear_cache()

    def test_shared_persona_no_second_block(self):
        self._persona("ops")
        memory_rules.clear_cache()
        with patch.dict(os.environ, {"AGENTS_PERSONA": "ops"}):
            with patch.object(memory_rules, "_via_import", return_value="<memory_rules>\n- s\n</memory_rules>"):
                with patch.object(memory_rules, "_via_cli", return_value="<memory_rules>\n- s\n</memory_rules>"):
                    block = memory_rules.memory_rules_prompt_block("p", workspace=self.ws)
        self.assertIn("shared" if "shared" in block else "- s", block)
        self.assertNotIn("private memory", block)
        memory_rules.clear_cache()

    def test_delegate_env_gets_private_root(self):
        self._persona("cos", "private")
        env = {}
        captured = {}

        class FakePopen:
            def __init__(self, argv, **kwargs):
                captured.update(kwargs.get("env") or {})
                self.pid = 1

            def poll(self):
                return 0

        import runner.delegate as delegate

        ws_env = {
            "AGENTS_HOST_DIR": str(self.host.dir),
            "AGENTS_WORKSPACE": "ws1",
            "AGENTS_DELEGATES_DIR": str(Path(self._tmp.name) / "dlg"),
        }
        with patch.dict(os.environ, ws_env, clear=True):
            with patch.object(delegate.subprocess, "Popen", FakePopen):
                delegate.spawn("t", provider="echo", persona="cos")
        self.assertEqual(
            captured.get("AGENTS_MEMORY_PATH"),
            str(self.ws.personas / "cos" / "memory"),
        )


if __name__ == "__main__":
    unittest.main()
