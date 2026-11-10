"""Per-call workspace isolation: explicit Workspace objects, no env mutation."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from agents_workspace import Host

from runner import modules as modules_mod
from runner.schedule import (
    add_schedule,
    list_dynamic_schedules,
    remove_schedule,
    tick,
)
from runner.loop import main as loop_main


class TestWorkspaceParam(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_ws_iso_")
        self.host = Host.open(self.tmp)
        self.ws_a = self.host.create("a")
        self.ws_b = self.host.create("b")

    def test_schedule_lands_in_its_workspace(self):
        add_schedule(
            name="only_a",
            at="+1h",
            verb=f'{sys.executable} -c "print(1)"',
            workspace=self.ws_a,
        )
        rows_a = list_dynamic_schedules(self.ws_a)
        rows_b = list_dynamic_schedules(self.ws_b)
        self.assertEqual([r["name"] for r in rows_a], ["only_a"])
        self.assertEqual(rows_b, [])
        self.assertTrue(
            (self.ws_a.schedules / "only_a.json").is_file(),
            "manifest must land under <host>/workspaces/a/schedules",
        )
        self.assertTrue(remove_schedule("only_a", self.ws_a))

    def test_modules_overlay_per_workspace(self):
        mod = {"name": "mod_a", "kind": "schedule", "when": "later",
               "cadence": "x", "verb": "true", "rests_on": "t"}
        self.ws_a.modules.mkdir(parents=True, exist_ok=True)
        (self.ws_a.modules / "mod_a.json").write_text(
            json.dumps(mod), encoding="utf-8"
        )
        names_a = {m["name"] for m in modules_mod.list_modules(workspace=self.ws_a)}
        names_b = {m["name"] for m in modules_mod.list_modules(workspace=self.ws_b)}
        self.assertIn("mod_a", names_a)
        self.assertNotIn("mod_a", names_b)

    def test_tick_uses_only_its_workspace(self):
        marker = Path(self.tmp) / "tick_a.txt"
        add_schedule(
            name="tick_a",
            at="2000-01-01T00:00:00Z",
            verb=f'python -c "open(r\'{marker}\',\'w\').write(\'x\')"',
            one_shot=True,
            workspace=self.ws_a,
        )
        results = tick(workspace=self.ws_a)
        self.assertTrue(marker.is_file(), results)
        # b's tick sees nothing
        self.assertEqual(tick(workspace=self.ws_b), [])

    def test_execute_job_env_comes_from_workspace(self):
        from runner.executor import execute_job

        manifest = {
            "name": "probe",
            "verb": "python -c \"import os;print(os.environ.get('AGENTS_WORKSPACE',''))\"",
            "cadence": "on_request",
            "rests_on": "probe",
            "expected_exit": 0,
        }
        res = execute_job(manifest, workspace=self.ws_a)
        self.assertEqual(res.get("status"), "SUCCESS")
        self.assertIn("a", res.get("stdout", ""))

    def test_loop_rejects_unknown_workspace(self):
        code = loop_main(["--workspace", "nope", "--user", "u"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
