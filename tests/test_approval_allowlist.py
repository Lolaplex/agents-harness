"""Approval pattern allowlist: approved patterns skip later prompts."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from runner import approval_allowlist as al
from runner.approval import gate_module

_KEYS = (
    "AGENTS_APPROVAL_CMD",
    "AGENTS_APPROVAL_MODE",
    "AGENTS_APPROVAL_ALLOWLIST",
    "AGENTS_APPROVAL_RULES",
    "AGENTS_APPROVAL_POLICY",
)


def _clean() -> dict:
    saved = {k: os.environ.get(k) for k in _KEYS}
    for k in _KEYS:
        os.environ.pop(k, None)
    return saved


def _restore(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class TestAllowlist(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = _clean()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name) / "allowlist.json"
        os.environ["AGENTS_APPROVAL_ALLOWLIST"] = str(self.store)
        self.marker = Path(self._tmp.name) / "approver-hits.txt"
        # Approver: logs a hit, prints {"remember": true}, exits 0 (approve).
        script = Path(self._tmp.name) / "approver.py"
        script.write_text(
            "import os,pathlib,sys\n"
            "sys.stdin.read()\n"
            "pathlib.Path(os.environ['APPROVER_MARKER']).open('a').write('hit\\n')\n"
            "print('{\"remember\": true}')\n"
        )
        os.environ["APPROVER_MARKER"] = str(self.marker)
        os.environ["AGENTS_APPROVAL_CMD"] = f'"{sys.executable}" "{script}"'
        os.environ["AGENTS_APPROVAL_MODE"] = "ask"

    def tearDown(self) -> None:
        _restore(self._saved)

    def _hits(self) -> int:
        try:
            return len(self.marker.read_text().strip().splitlines())
        except OSError:
            return 0

    def test_remember_persists_and_skips(self) -> None:
        mod = {"name": "mcp.terminal", "mutates": True}
        args = {"argv": ["git", "status"]}
        allowed, _ = gate_module(mod, args)
        self.assertTrue(allowed)
        self.assertEqual(self._hits(), 1)
        # Second identical call: allowlist match, no prompt.
        allowed, _ = gate_module(mod, args)
        self.assertTrue(allowed)
        self.assertEqual(self._hits(), 1)
        pats = al.load_patterns()
        self.assertEqual(len(pats), 1)

    def test_explicit_pattern_matches_summary(self) -> None:
        al.remember("mcp.terminal", {}, pattern="mcp.terminal *git status*")
        self.assertTrue(al.is_allowed("mcp.terminal", {"argv": ["git", "status"]}))
        self.assertFalse(al.is_allowed("mcp.terminal", {"argv": ["git", "push"]}))

    def test_off_env_disables(self) -> None:
        os.environ["AGENTS_APPROVAL_ALLOWLIST"] = "off"
        self.assertIsNone(al.store_path())
        self.assertEqual(al.load_patterns(), [])

    def test_default_path_under_host(self) -> None:
        os.environ.pop("AGENTS_APPROVAL_ALLOWLIST", None)
        try:
            from agents_workspace import Host

            host = Host.from_env()
        except Exception:
            self.skipTest("no host")
        path = al.store_path()
        self.assertIsNotNone(path)
        self.assertEqual(path.parent, Path(host.agents_json).parent)

    def test_deny_rule_still_wins(self) -> None:
        al.remember("mcp.terminal", {"argv": ["git", "status"]})
        os.environ["AGENTS_APPROVAL_RULES"] = json.dumps(
            [{"action": "tool:mcp.terminal", "decision": "deny"}]
        )
        mod = {"name": "mcp.terminal", "mutates": True}
        allowed, denial = gate_module(mod, {"argv": ["git", "status"]})
        self.assertFalse(allowed)
        self.assertIn("denied", denial.lower())


if __name__ == "__main__":
    unittest.main()
