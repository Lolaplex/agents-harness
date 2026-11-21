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

    def test_depth_guard_blocks_nested_spawn(self) -> None:
        from unittest.mock import patch

        with patch.dict(os.environ, {"AGENTS_DELEGATE_DEPTH": "1"}):
            with self.assertRaises(delegate.DelegateError):
                delegate.spawn("nested")

    def test_ledger_written_and_counts_toward_depth(self) -> None:
        # spawn at depth 0 writes the ledger; a later process reading the
        # ledger (env counter forged to 0) is still refused at MAX_DEPTH=1.
        spawned = delegate.spawn("ledger-probe", provider="echo")
        try:
            self.assertEqual(
                delegate._ledger_path().read_text(encoding="utf-8").strip(), "1"
            )
            os.environ.pop("AGENTS_DELEGATE_DEPTH", None)
            with self.assertRaises(delegate.DelegateError):
                delegate.spawn("nested-ledger")
        finally:
            delegate.result(spawned["id"], timeout_sec=120)

    def test_workdir_escape_refused(self) -> None:
        with self.assertRaises(delegate.DelegateError):
            delegate.spawn("x", workdir="C:\\Windows" if os.name == "nt" else "/etc")

    def test_workdir_inside_workspace_accepted(self) -> None:
        from agents_workspace import Workspace

        ws = Workspace.from_env()
        spawned = delegate.spawn("echo-wd", provider="echo", workdir=str(ws.dir))
        try:
            out = delegate.result(spawned["id"], timeout_sec=120)
            self.assertEqual(out["state"], "exited")
            self.assertEqual(out["exit_code"], 0)
        finally:
            delegate.cancel(spawned["id"])

    def test_denied_paths_refused_before_allowed_roots(self) -> None:
        from pathlib import Path
        from unittest.mock import patch
        from agents_workspace import Host, denied_paths

        with tempfile.TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            ws = host.create("demo")
            (host.keys / "sub").mkdir(parents=True, exist_ok=True)
            (host.dir / "mcp").mkdir(parents=True, exist_ok=True)
            (host.dir / "secrets").mkdir(parents=True, exist_ok=True)
            with patch.dict(
                os.environ,
                {
                    "AGENTS_HOST_DIR": str(host.dir),
                    "AGENTS_WORKSPACE": "demo",
                    # Even explicit allow-listing must not override denied_paths.
                    "AGENTS_WORKDIR_ALLOW": os.pathsep.join(
                        [str(host.keys), str(ws.secrets), str(host.dir / "mcp")]
                    ),
                },
                clear=False,
            ):
                targets = list(denied_paths(ws)) + [
                    host.keys / "sub",
                    host.dir / "mcp" / "ws-demo.json",
                    host.dir / "secrets" / "app.json",
                ]
                for target in targets:
                    with self.assertRaises(delegate.DelegateError, msg=str(target)):
                        delegate.spawn("x", workdir=str(target))
                self.assertEqual(delegate._checked_workdir(str(ws.dir)), str(ws.dir.resolve()))
                self.assertEqual(delegate._checked_workdir(str(ws.workdir)), str(ws.workdir.resolve()))

    def test_dash_task_not_parsed_as_flag(self) -> None:
        """A task starting with '-' must reach the child as a value, not an option."""
        spawned = delegate.spawn("--verbose marker", provider="echo")
        out = delegate.result(spawned["id"], timeout_sec=120)
        self.assertEqual(out["state"], "exited")
        self.assertEqual(out["exit_code"], 0)


class TestDelegateDeclaredCommands(unittest.TestCase):
    """commands_from: help-json wires the module without a cordis_tools shim."""

    def test_declared_argv(self) -> None:
        from runner.helpjson import declared_argv, with_discovered_commands
        from runner.modules import find_module

        mod = with_discovered_commands(find_module("mcp.delegate"))
        self.assertEqual(
            declared_argv(mod, {"command": "spawn", "task": "say hi"}),
            ["spawn", "--task", "say hi"],
        )
        self.assertEqual(
            declared_argv(mod, {"command": "status", "id": "dlg_x"}),
            ["status", "dlg_x"],
        )
        self.assertEqual(
            declared_argv(mod, {"command": "result", "id": "dlg_x", "timeout": 30, "tail": 50}),
            ["result", "dlg_x", "--timeout", "30", "--tail", "50"],
        )
        self.assertEqual(declared_argv(mod, {"command": "cancel", "id": "dlg_x"}), ["cancel", "dlg_x"])

    def test_declared_effects(self) -> None:
        from runner.helpjson import call_effects, with_discovered_commands
        from runner.modules import find_module

        mod = with_discovered_commands(find_module("mcp.delegate"))
        self.assertEqual(call_effects(mod, {"command": "spawn"}), "mutating")
        self.assertEqual(call_effects(mod, {"command": "result"}), "read-only")


if __name__ == "__main__":
    unittest.main()
