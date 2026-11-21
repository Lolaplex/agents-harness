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

    def test_default_dir_uses_ws_delegates_and_run_ledger(self) -> None:
        from pathlib import Path
        from unittest.mock import patch
        from agents_workspace import Host

        with tempfile.TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            ws = host.create("demo")
            self.assertFalse(ws.run.exists())
            self.assertFalse((ws.dir / "delegates").exists())
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("AGENTS_DELEGATES_DIR", "AGENTS_DELEGATE_DEPTH", "AGENTS_WORKSPACE_ROOT")
            }
            env["AGENTS_HOST_DIR"] = str(host.dir)
            env["AGENTS_WORKSPACE"] = "demo"
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(delegate._root(), ws.delegates)
                self.assertEqual(delegate._ledger_path(), ws.run / "delegate.depth")
                spawned = delegate.spawn("ws-run-delegates", provider="echo")
                dlg_id = spawned["id"]
                self.assertTrue((ws.delegates / dlg_id / "meta.json").is_file())
                self.assertFalse((ws.dir / "delegates").exists())
                self.assertEqual(
                    (ws.run / "delegate.depth").read_text(encoding="utf-8").strip(),
                    "1",
                )
                self.assertFalse((ws.run / "run" / "delegate.depth").exists())
                out = delegate.result(dlg_id, timeout_sec=120)
                self.assertEqual(out["state"], "exited")
                self.assertEqual(out["exit_code"], 0)
                self.assertIn("ws-run-delegates", out["stdout"])

    def test_legacy_delegates_dir_read_fallback(self) -> None:
        import json
        from pathlib import Path
        from unittest.mock import patch
        from agents_workspace import Host

        with tempfile.TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            ws = host.create("demo")
            legacy_id = "dlg_legacy01"
            legacy_dir = ws.dir / "delegates" / legacy_id
            legacy_dir.mkdir(parents=True, exist_ok=True)
            (legacy_dir / "meta.json").write_text(
                json.dumps({"id": legacy_id, "task": "legacy-task", "session": "ses_dlg_legacy01", "pid": 0}),
                encoding="utf-8",
            )
            (legacy_dir / "out.log").write_text("legacy stdout\n", encoding="utf-8")
            (legacy_dir / "err.log").write_text("", encoding="utf-8")
            (legacy_dir / "exit.json").write_text(
                json.dumps({"code": 0, "finished": "2026-11-21T00:00:00+00:00"}),
                encoding="utf-8",
            )
            new_id = "dlg_current01"
            new_dir = ws.delegates / new_id
            new_dir.mkdir(parents=True, exist_ok=True)
            (new_dir / "meta.json").write_text(
                json.dumps({"id": new_id, "task": "new-task", "session": "ses_dlg_current01", "pid": 0}),
                encoding="utf-8",
            )
            (new_dir / "exit.json").write_text(
                json.dumps({"code": 0, "finished": "2026-11-21T00:00:00+00:00"}),
                encoding="utf-8",
            )

            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("AGENTS_DELEGATES_DIR", "AGENTS_WORKSPACE_ROOT")
            }
            env["AGENTS_HOST_DIR"] = str(host.dir)
            env["AGENTS_WORKSPACE"] = "demo"
            with patch.dict(os.environ, env, clear=True):
                st = delegate.status(legacy_id)
                self.assertEqual(st["state"], "exited")
                self.assertEqual(st["exit_code"], 0)
                res = delegate.result(legacy_id, timeout_sec=5)
                self.assertIn("legacy stdout", res["stdout"])
                self.assertEqual(delegate.cancel(legacy_id)["exit_code"], 0)
                ids = [row["id"] for row in delegate.list_runs()["delegates"]]
                self.assertEqual(ids, [new_id, legacy_id])

            # With explicit AGENTS_DELEGATES_DIR override, legacy <ws>/delegates is not read.
            with patch.dict(
                os.environ,
                {**env, "AGENTS_DELEGATES_DIR": str(Path(tmp) / "custom-dlg")},
                clear=True,
            ):
                with self.assertRaises(delegate.DelegateError):
                    delegate.status(legacy_id)
                self.assertEqual(delegate.list_runs()["delegates"], [])

    def test_sequential_spawns_succeed_after_exit(self) -> None:
        import json
        from pathlib import Path
        from unittest.mock import patch
        from agents_workspace import Host

        with tempfile.TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            ws = host.create("demo")
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("AGENTS_DELEGATES_DIR", "AGENTS_DELEGATE_DEPTH", "AGENTS_WORKSPACE_ROOT")
            }
            env["AGENTS_HOST_DIR"] = str(host.dir)
            env["AGENTS_WORKSPACE"] = "demo"
            with patch.dict(os.environ, env, clear=True):
                first = delegate.spawn("first-seq-task", provider="echo")
                first_meta = json.loads((ws.delegates / first["id"] / "meta.json").read_text(encoding="utf-8"))
                self.assertEqual(first_meta.get("depth"), 1)
                out1 = delegate.result(first["id"], timeout_sec=120)
                self.assertEqual(out1["state"], "exited")
                self.assertEqual(out1["exit_code"], 0)
                self.assertFalse((ws.run / "delegate.depth").exists())

                second = delegate.spawn("second-seq-task", provider="echo")
                out2 = delegate.result(second["id"], timeout_sec=120)
                self.assertEqual(out2["state"], "exited")
                self.assertEqual(out2["exit_code"], 0)
                self.assertIn("second-seq-task", out2["stdout"])
                self.assertFalse((ws.run / "delegate.depth").exists())

                # Dead supervisor PID without exit.json must not DoS future spawns.
                crashed = ws.delegates / "dlg_crashed"
                crashed.mkdir(parents=True, exist_ok=True)
                (crashed / "meta.json").write_text(
                    json.dumps({"id": "dlg_crashed", "task": "crashed", "session": "ses_crashed", "pid": 999999999, "depth": 1}),
                    encoding="utf-8",
                )
                delegate._write_ledger(1)
                third = delegate.spawn("after-crash-task", provider="echo")
                out3 = delegate.result(third["id"], timeout_sec=120)
                self.assertEqual(out3["state"], "exited")
                self.assertEqual(out3["exit_code"], 0)

                # cancel() clears the ledger when a live delegate is cancelled.
                live_dir = ws.delegates / "dlg_live"
                live_dir.mkdir(parents=True, exist_ok=True)
                (live_dir / "meta.json").write_text(
                    json.dumps({"id": "dlg_live", "task": "live", "session": "ses_live", "pid": 123456, "depth": 1}),
                    encoding="utf-8",
                )
                delegate._write_ledger(1)
                with patch.object(
                    delegate, "_pid_alive", side_effect=lambda p: p == 123456
                ), patch.object(delegate, "_kill_tree"):
                    cancelled = delegate.cancel("dlg_live")
                self.assertEqual(cancelled["state"], "exited")
                self.assertFalse((ws.run / "delegate.depth").exists())

    def test_spawn_strips_infra_secrets(self) -> None:
        import json
        from pathlib import Path
        from unittest.mock import patch
        from agents_workspace import INFRA_SECRET_KEYS, Host

        with tempfile.TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            ws = host.create("demo")
            captured: dict[str, str] = {}

            class FakePopen:
                def __init__(self, argv, **kwargs):
                    captured.clear()
                    captured.update(kwargs.get("env") or {})
                    self.pid = 424242

            base_env = {
                "AGENTS_HOST_DIR": str(host.dir),
                "AGENTS_WORKSPACE": "demo",
                "AGENTS_DIR": str(Path(tmp) / "legacy"),
                "AGENTS_SYNC_TOKEN": "sync-secret",
                "LLM_API_KEY": "llm-keep",
                "RELAY_SECRET": "relay-secret",
                "TELEGRAM_BOT_TOKEN": "tg-secret",
            }
            for key in INFRA_SECRET_KEYS:
                base_env[key] = f"secret-{key}"

            with patch.dict(os.environ, base_env, clear=True):
                with patch.object(delegate.subprocess, "Popen", FakePopen):
                    spawned = delegate.spawn("strip-check", provider="echo", workdir=str(ws.workdir))
                for key in INFRA_SECRET_KEYS:
                    self.assertNotIn(key, captured)
                self.assertNotIn("AGENTS_SYNC_TOKEN", captured)
                self.assertNotIn("AGENTS_DIR", captured)
                self.assertEqual(captured.get("LLM_API_KEY"), "llm-keep")
                self.assertEqual(captured.get("AGENTS_WORKDIR"), str(ws.workdir.resolve()))
                self.assertEqual(captured.get("AGENTS_WORKDIR_LOCK"), "1")
                self.assertEqual(captured.get("AGENTS_DELEGATE"), spawned["id"])
                self.assertEqual(captured.get("AGENTS_DELEGATE_DEPTH"), "1")
                meta = json.loads((ws.delegates / spawned["id"] / "meta.json").read_text(encoding="utf-8"))
                self.assertEqual(meta.get("depth"), 1)

                # Nested spawn with MAX_DEPTH=2 and forged env depth=0 respects ledger=1 -> depth=2 (no double-count).
                with patch.dict(os.environ, {"AGENTS_DELEGATE_MAX_DEPTH": "2", "AGENTS_DELEGATE_DEPTH": "0"}):
                    with patch.object(delegate, "_pid_alive", side_effect=lambda p: p == 424242):
                        with patch.object(delegate.subprocess, "Popen", FakePopen):
                            nested = delegate.spawn("nested-check", provider="echo")
                self.assertEqual(captured.get("AGENTS_DELEGATE_DEPTH"), "2")
                nested_meta = json.loads((ws.delegates / nested["id"] / "meta.json").read_text(encoding="utf-8"))
                self.assertEqual(nested_meta.get("depth"), 2)

            # Standalone AGENTS_DELEGATES_DIR with empty host must not create a workspace and still strips secrets.
            empty_host = Path(tmp) / "empty-host"
            empty_host.mkdir()
            standalone_dlg = Path(tmp) / "standalone-dlg"
            standalone_env = {
                "AGENTS_HOST_DIR": str(empty_host),
                "AGENTS_DELEGATES_DIR": str(standalone_dlg),
                "RELAY_SECRET": "relay-secret",
                "TELEGRAM_BOT_TOKEN": "tg-secret",
                "AGENTS_SYNC_TOKEN": "sync-secret",
                "AGENTS_DIR": str(Path(tmp) / "legacy"),
            }
            with patch.dict(os.environ, standalone_env, clear=True):
                with patch.object(delegate.subprocess, "Popen", FakePopen):
                    delegate.spawn("standalone-check", provider="echo")
                self.assertNotIn("RELAY_SECRET", captured)
                self.assertNotIn("TELEGRAM_BOT_TOKEN", captured)
                self.assertNotIn("AGENTS_SYNC_TOKEN", captured)
                self.assertNotIn("AGENTS_DIR", captured)
                self.assertEqual(captured.get("AGENTS_DELEGATE_DEPTH"), "1")
                self.assertFalse((empty_host / "workspaces").exists())


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
