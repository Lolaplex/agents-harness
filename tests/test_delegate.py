"""Delegate subagent turns: spawn, result, status, cancel."""

import os
import tempfile
import unittest

from runner import delegate


class TestDelegate(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.environ["AGENTS_DELEGATES_DIR"] = self._tmp.name

    def tearDown(self) -> None:
        os.environ.pop("AGENTS_DELEGATES_DIR", None)

    def test_spawn_result_echo(self) -> None:
        spawned = delegate.spawn("echo-task-check", provider="echo")
        dlg_id = spawned["id"]
        self.assertTrue(dlg_id.startswith("dlg_"))
        self.assertTrue(spawned["session"].startswith("ses_dlg_"))
        out = delegate.result(dlg_id, timeout_sec=120)
        self.assertEqual(out["state"], "exited")
        self.assertEqual(out["exit_code"], 0)
        self.assertIn("echo-task-check", out["stdout"])

    def test_list_and_status(self) -> None:
        spawned = delegate.spawn("noop", provider="echo")
        dlg_id = spawned["id"]
        try:
            st = delegate.status(dlg_id)
            self.assertIn(st["state"], ("running", "exited"))
            ids = [r["id"] for r in delegate.list_runs()["delegates"]]
            self.assertIn(dlg_id, ids)
        finally:
            delegate.result(dlg_id, timeout_sec=120)

    def test_unknown_id(self) -> None:
        with self.assertRaises(delegate.DelegateError):
            delegate.status("dlg_missing")

    def test_spawn_requires_task(self) -> None:
        with self.assertRaises(delegate.DelegateError):
            delegate.spawn("   ")


if __name__ == "__main__":
    unittest.main()
