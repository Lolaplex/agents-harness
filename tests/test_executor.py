"""Unit and integration tests for agents-harness runner executor"""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_workspace import Workspace

from runner.executor import execute_job, list_schedules, load_manifest, traces_dir


class TestWorkspaceJob(unittest.TestCase):
    def test_execute_job_uses_workspace_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "print_env.py"
            script.write_text(
                "import os;print(os.environ.get('AGENTS_MEMORY_PATH',''))",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"AGENTS_WORKSPACES_DIR": tmp, "AGENTS_TRACES_DIR": str(Path(tmp) / "traces")},
                clear=False,
            ):
                ws = Workspace.create("demo")
                manifest = {
                    "name": "envcheck",
                    "verb": f'python "{script}"',
                    "rests_on": "test",
                    "cadence": "one_shot",
                    "expected_exit": 0,
                    "timeout_sec": 20,
                    "workspace": "demo",
                }
                result = execute_job(manifest, log_path=None)
            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(result["stdout"].strip(), str(ws.memory))

    def test_execute_job_rejects_unknown_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {"AGENTS_WORKSPACES_DIR": tmp, "AGENTS_TRACES_DIR": str(Path(tmp) / "traces")},
                clear=False,
            ):
                manifest = {
                    "name": "badws",
                    "verb": "python -c \"print('x')\"",
                    "rests_on": "test",
                    "cadence": "one_shot",
                    "expected_exit": 0,
                    "timeout_sec": 20,
                    "workspace": "nope",
                }
                result = execute_job(manifest, log_path=None)
            self.assertEqual(result["status"], "ERROR")
            self.assertIn("unknown workspace", result["stderr"])


class TestHarnessExecutor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.schedules_dir = Path(self.temp_dir.name) / "schedules"
        self.schedules_dir.mkdir()
        self.log_file = Path(self.temp_dir.name) / "runner.jsonl"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_load_manifest_valid(self):
        manifest_path = self.schedules_dir / "valid.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({
                "name": "test_echo",
                "verb": "python -c \"print('hello')\"",
                "cadence": "hourly",
                "rests_on": "standard python print, exit 0 = healthy",
                "expected_exit": 0,
                "timeout_sec": 5
            }, f)

        data = load_manifest(manifest_path)
        self.assertEqual(data["name"], "test_echo")
        self.assertEqual(data["expected_exit"], 0)

    def test_load_manifest_missing_required(self):
        manifest_path = self.schedules_dir / "invalid.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({
                "name": "incomplete"
            }, f)

        with self.assertRaises(ValueError):
            load_manifest(manifest_path)

    def test_execute_job_success(self):
        manifest = {
            "name": "quick_echo",
            "verb": "python -c \"print('harness ok')\"",
            "cadence": "on_boot",
            "rests_on": "python stdlib print, exit 0 = healthy",
            "expected_exit": 0,
            "timeout_sec": 10,
            "cwd": None,
        }
        res = execute_job(manifest, log_path=self.log_file)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("harness ok", res["stdout_tail"])
        self.assertTrue(self.log_file.exists())

        # Verify JSONL record format
        with open(self.log_file, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["job"], "quick_echo")
        self.assertEqual(lines[0]["status"], "SUCCESS")
        self.assertNotIn("stdout", lines[0])
        self.assertNotIn("stderr", lines[0])

    def test_execute_job_log_keeps_tail_and_returns_full_stdout(self):
        body = "S" + ("a" * 2500) + "E"
        manifest = {
            "name": "long_echo",
            "verb": 'python -c "import sys; sys.stdout.write(%s)"' % repr(body),
            "cadence": "on_boot",
            "rests_on": "python stdlib write, exit 0 = healthy",
            "expected_exit": 0,
            "timeout_sec": 10,
            "cwd": None,
        }
        res = execute_job(manifest, log_path=self.log_file)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["stdout"], body)
        self.assertEqual(res["stderr"], "")
        self.assertEqual(res["stdout_tail"], body[-1000:])
        self.assertEqual(len(res["stdout_tail"]), 1000)

        with open(self.log_file, "r", encoding="utf-8") as f:
            logged = json.loads(f.readline())
        self.assertNotIn("stdout", logged)
        self.assertNotIn("stderr", logged)
        self.assertEqual(logged["stdout_tail"], body[-1000:])
        self.assertFalse(logged["stdout_tail"].startswith("S"))
        self.assertTrue(logged["stdout_tail"].endswith("E"))

    def test_execute_job_failure_exit_code(self):
        manifest = {
            "name": "failing_job",
            "verb": "python -c \"import sys; sys.exit(42)\"",
            "cadence": "daily",
            "rests_on": "intentional exit 42",
            "expected_exit": 0,
            "timeout_sec": 10,
            "cwd": None,
        }
        res = execute_job(manifest, log_path=self.log_file)
        self.assertEqual(res["status"], "FAILED")
        self.assertEqual(res["exit_code"], 42)

    def test_bundled_schedules_disabled(self):
        with patch.dict(os.environ, {"AGENTS_BUNDLED_SCHEDULES": "0"}, clear=False):
            manifests = list_schedules()
        names = {m["name"] for m in manifests}
        self.assertNotIn("traces_ingest", names)

    def test_all_bundled_manifests_valid(self):
        manifests = list_schedules()
        self.assertGreaterEqual(len(manifests), 4)
        names = {m["name"] for m in manifests}
        self.assertIn("memory_inventory", names)
        self.assertNotIn("docs_catalog", names)
        self.assertIn("traces_stats", names)
        self.assertIn("traces_ingest", names)

    def test_execute_job_honors_agents_traces_dir(self):
        unique = f"env_traces_{os.getpid()}_{time.time_ns()}"
        live_dir = Path.home() / ".agents" / "traces"
        live_file = live_dir / "runner.jsonl"
        manifest = {
            "name": unique,
            "verb": "python -c \"print('traces-env')\"",
            "cadence": "on_boot",
            "rests_on": "cheap python print, exit 0 = healthy",
            "expected_exit": 0,
            "timeout_sec": 10,
            "cwd": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp) / "sandbox-traces"
            with patch.dict(os.environ, {"AGENTS_TRACES_DIR": str(sandbox)}):
                got = traces_dir()
                self.assertEqual(got, sandbox.expanduser().resolve())
                self.assertNotEqual(got, live_dir.resolve())
                res = execute_job(manifest, log_path=None)
            dest = sandbox / "runner.jsonl"
            self.assertEqual(res["status"], "SUCCESS")
            self.assertTrue(dest.is_file())
            self.assertIn(unique, dest.read_text(encoding="utf-8"))
        if live_file.exists():
            self.assertNotIn(unique, live_file.read_text(encoding="utf-8"))


class TestWorkspaceLock(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.root = self.tmp / "ws"
        self.outside = self.tmp / "out"
        self.root.mkdir()
        self.outside.mkdir()
        (self.root / "sub").mkdir()
        self.inside = self.root / "ok.txt"
        self.inside.write_text("inside", encoding="utf-8")
        self.secret = self.outside / "secret.txt"
        self.secret.write_text("TOPSECRET", encoding="utf-8")
        self.leakdir = self.root / "leakdir"
        self.indir = self.root / "indir"
        self.cwd_link = self.root / "out-link"
        self._reparse(self.outside, self.leakdir)
        self._reparse(self.root, self.indir)
        self._reparse(self.outside, self.cwd_link)
        self.log = self.tmp / "runner.jsonl"

    def tearDown(self) -> None:
        for path in (self.leakdir, self.indir, self.cwd_link):
            if path.is_symlink() or path.exists():
                path.unlink()
        self._td.cleanup()

    def _reparse(self, src: Path, dst: Path) -> None:
        try:
            os.symlink(src, dst, target_is_directory=True)
        except OSError:
            subprocess.check_call(
                ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _env(self, locked: bool, **extra: str):
        env = os.environ.copy()
        for key in (
            "AGENTS_WORKSPACE_DIR",
            "AGENTS_WORKSPACE_LOCK",
            "AGENTS_WORKSPACE_ALLOW",
            "AGENTS_HOME",
            "AGENTS_SKILLS_DIR",
            "AGENTS_SKILLS_EXTRA",
            "AGENTS_MODULES_DIR",
            "AGENTS_MEMORY_PATH",
            "AGENTS_TRACES_DIR",
            "AGENTS_SCHEDULES_DIR",
            "AGENTS_ROUTINE_RUNS_DIR",
        ):
            env.pop(key, None)
        if locked:
            env["AGENTS_WORKSPACE_DIR"] = str(self.root)
            env["AGENTS_WORKSPACE_LOCK"] = "1"
        env.update(extra)
        return patch.dict(os.environ, env, clear=True)

    def _job(self, verb: str, cwd: str | None) -> dict:
        return {
            "name": "cmd",
            "verb": verb,
            "cadence": "on_request",
            "rests_on": "workspace lock test",
            "expected_exit": 0,
            "timeout_sec": 15,
            "cwd": cwd,
        }

    def _write(self, name: str, cwd: Path):
        verb = f"python -c \"open('{name}','w',encoding='utf-8').write('x')\""
        return execute_job(self._job(verb, str(cwd)), log_path=self.log)

    def test_inside_allowed(self) -> None:
        verb = "python -c \"print('ok')\""
        with self._env(True):
            direct = execute_job(self._job(verb, str(self.root)), log_path=self.log)
            nested = execute_job(self._job(verb, str(self.root / "sub")), log_path=self.log)
        self.assertEqual(direct["status"], "SUCCESS", direct["stderr"])
        self.assertEqual(nested["status"], "SUCCESS", nested["stderr"])

    def test_outside_cwd_refused(self) -> None:
        missing = self.tmp / "missing-cwd"
        canary = self.outside / "cwd-canary.txt"
        with self._env(True):
            missing_result = self._write("cwd-canary.txt", missing)
            outside_result = self._write("cwd-canary.txt", self.outside)
            link_result = self._write("cwd-canary.txt", self.cwd_link)
        for result in (missing_result, outside_result, link_result):
            self.assertEqual(result["status"], "ERROR")
            self.assertEqual(result["stderr"], "path escapes workspace root")
        self.assertFalse(missing.exists())
        self.assertFalse(canary.exists())

    def test_internal_dirs_stay_open(self) -> None:
        home = self.tmp / "instance"
        named = {
            "AGENTS_SKILLS_DIR": self.tmp / "skills",
            "AGENTS_MODULES_DIR": self.tmp / "modules",
            "AGENTS_MEMORY_PATH": self.tmp / "memory",
            "AGENTS_TRACES_DIR": self.tmp / "traces",
            "AGENTS_SCHEDULES_DIR": self.tmp / "schedules",
        }
        home.mkdir()
        for folder in named.values():
            folder.mkdir()
        verb = "python -c \"print('ok')\""
        with self._env(True, AGENTS_HOME=str(home), **{key: str(path) for key, path in named.items()}):
            home_cwd = execute_job(self._job(verb, str(home)), log_path=self.log)
            self.assertEqual(home_cwd["status"], "SUCCESS", home_cwd["stderr"])
            for folder in named.values():
                result = execute_job(self._job(verb, str(folder)), log_path=self.log)
                self.assertEqual(result["status"], "SUCCESS", result["stderr"])

    def test_allow_list_opens_extra_root(self) -> None:
        verb = "python -c \"print('ok')\""
        with self._env(True, AGENTS_WORKSPACE_ALLOW=str(self.outside)):
            result = execute_job(self._job(verb, str(self.outside)), log_path=self.log)
        self.assertEqual(result["status"], "SUCCESS", result["stderr"])

    def test_arguments_are_not_interpreted(self) -> None:
        verb = "python -c \"import sys; print(sys.argv[1])\""
        text = "PR nach dev, branch feat/x, 09/11/2026"
        with self._env(True):
            result = execute_job(self._job(verb, str(self.root)), extra_argv=[text], log_path=self.log)
        self.assertEqual(result["status"], "SUCCESS", result["stderr"])
        self.assertEqual(result["stdout"], text)

    def test_dir_alone_does_not_lock(self) -> None:
        canary = self.outside / "dir-canary.txt"
        with self._env(False, AGENTS_WORKSPACE_DIR=str(self.root)):
            result = self._write("dir-canary.txt", self.outside)
        self.assertEqual(result["status"], "SUCCESS", result["stderr"])
        self.assertEqual(canary.read_text(encoding="utf-8"), "x")

    def test_blank_lock_is_off(self) -> None:
        canary = self.outside / "blank-canary.txt"
        with self._env(False, AGENTS_WORKSPACE_DIR=str(self.root), AGENTS_WORKSPACE_LOCK="   "):
            result = self._write("blank-canary.txt", self.outside)
        self.assertEqual(result["status"], "SUCCESS", result["stderr"])
        self.assertEqual(canary.read_text(encoding="utf-8"), "x")

    def test_unset_unchanged(self) -> None:
        canary = self.outside / "unset-canary.txt"
        with self._env(False):
            result = self._write("unset-canary.txt", self.outside)
        self.assertEqual(result["status"], "SUCCESS", result["stderr"])
        self.assertEqual(canary.read_text(encoding="utf-8"), "x")


if __name__ == "__main__":
    unittest.main()


class TestWorkdirAliases(unittest.TestCase):
    def test_workdir_lock_new_name(self):
        import os, warnings
        from runner import executor
        old_lock = os.environ.pop("AGENTS_WORKSPACE_LOCK", None)
        old_new = os.environ.pop("AGENTS_WORKDIR_LOCK", None)
        try:
            os.environ["AGENTS_WORKDIR_LOCK"] = "1"
            self.assertTrue(executor._lock_on())
        finally:
            os.environ.pop("AGENTS_WORKDIR_LOCK", None)
            if old_lock is not None:
                os.environ["AGENTS_WORKSPACE_LOCK"] = old_lock
            if old_new is not None:
                os.environ["AGENTS_WORKDIR_LOCK"] = old_new

    def test_workspace_lock_alias_warns(self):
        import os, warnings
        from runner import executor
        old = os.environ.pop("AGENTS_WORKDIR_LOCK", None)
        try:
            os.environ["AGENTS_WORKSPACE_LOCK"] = "1"
            with warnings.catch_warnings(record=True) as seen:
                warnings.simplefilter("always")
                self.assertTrue(executor._lock_on())
            self.assertTrue(any("AGENTS_WORKSPACE_LOCK is deprecated" in str(w.message) for w in seen))
        finally:
            os.environ.pop("AGENTS_WORKSPACE_LOCK", None)
            if old is not None:
                os.environ["AGENTS_WORKDIR_LOCK"] = old
