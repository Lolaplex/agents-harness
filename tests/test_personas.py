"""C200: personas load from <ws>/personas/<id> first, package as fallback."""
import json
import tempfile
import unittest
from pathlib import Path

from agents_workspace import Host

from runner import personas


class PersonaLoading(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.host = Host(dir=Path(self._tmp.name) / "host")
        self.ws = self.host.create("demo")

    def tearDown(self):
        self._tmp.cleanup()

    def _persona(self, pid, text):
        d = self.ws.personas / pid
        d.mkdir(parents=True, exist_ok=True)
        (d / "persona.json").write_text(
            json.dumps({"id": pid, "system_append": text}), encoding="utf-8")

    def test_workspace_persona_beats_package_default(self):
        self._persona("default", "WS-RULE")
        out = personas.persona_system_append("default", base="B",
                                             workspace=self.ws)
        self.assertIn("WS-RULE", out)
        self.assertNotIn("helpful personal agent", out)

    def test_two_personas_equal_in_one_workspace(self):
        self._persona("cos", "CHIEF")
        self._persona("ops", "OPS")
        self.assertIn("CHIEF", personas.persona_system_append(
            "cos", workspace=self.ws))
        self.assertIn("OPS", personas.persona_system_append(
            "ops", workspace=self.ws))
        ids = {r["id"] for r in personas.list_personas(self.ws)}
        self.assertTrue({"cos", "ops"} <= ids)

    def test_package_fallback_for_unknown_id(self):
        row = personas.load_persona("default", workspace=self.ws)
        # ws default persona.json exists (created by Host.create)
        self.assertEqual(row["source"], "workspace")
        row = personas.load_persona("nonexistent-persona", workspace=self.ws)
        self.assertIsNone(row)

    def test_default_persona_created_with_workspace(self):
        self.assertTrue(
            (self.ws.personas / "default" / "persona.json").is_file())
        self.assertEqual(self.ws.default_persona, "default")

    def test_resolve_persona_id_order(self):
        self.assertEqual(
            personas.resolve_persona_id("cos", workspace=self.ws), "cos")
        self.assertEqual(
            personas.resolve_persona_id("", workspace=self.ws), "default")
        # default_persona is metadata, not a hierarchy: a ws whose first
        # persona is 'cos' resolves to it, no special-casing of 'default'
        ws2 = self.host.create("x", persona="cos")
        self.assertEqual(
            personas.resolve_persona_id("", workspace=ws2), "cos")


if __name__ == "__main__":
    unittest.main()
