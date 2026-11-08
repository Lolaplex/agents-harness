"""Unit and integration tests for agents-harness runner executor"""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.executor import execute_job, list_schedules, load_manifest, traces_dir


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
        self.assertIn("docs_catalog", names)
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

    def _read(self, path: str):
        verb = "python -c \"import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'), end='')\""
        with self._env(True):
            return execute_job(self._job(verb, str(self.root)), extra_argv=[path], log_path=self.log)

    def test_inside_allowed(self) -> None:
        direct = self._read(str(self.inside))
        via_link = self._read("indir/ok.txt")
        dotted = self._read("sub/../ok.txt")
        self.assertEqual(direct["status"], "SUCCESS", direct["stderr"])
        self.assertEqual(direct["stdout"], "inside")
        self.assertEqual(via_link["status"], "SUCCESS", via_link["stderr"])
        self.assertEqual(via_link["stdout"], "inside")
        self.assertEqual(dotted["status"], "SUCCESS", dotted["stderr"])
        self.assertEqual(dotted["stdout"], "inside")

    def test_outside_path_refused(self) -> None:
        absolute = self._read(str(self.secret))
        parent = self._read("../out/secret.txt")
        for result in (absolute, parent):
            self.assertEqual(result["status"], "ERROR")
            self.assertEqual(result["stderr"], "path escapes workspace root")
            self.assertNotIn("TOPSECRET", result["stdout"])

    def test_symlink_outside_refused(self) -> None:
        result = self._read("leakdir/secret.txt")
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["stderr"], "path escapes workspace root")
        self.assertNotIn("TOPSECRET", result["stdout"])
        self.assertEqual(self.secret.read_text(encoding="utf-8"), "TOPSECRET")

    def test_outside_cwd_refused(self) -> None:
        missing = self.tmp / "missing-cwd"
        canary = self.outside / "canary.txt"
        verb = "python -c \"open('canary.txt','w',encoding='utf-8').write('x')\""
        with self._env(True):
            missing_result = execute_job(self._job(verb, str(missing)), log_path=self.log)
            link_result = execute_job(self._job(verb, str(self.cwd_link)), log_path=self.log)
        self.assertEqual(missing_result["status"], "ERROR")
        self.assertFalse(missing.exists())
        self.assertEqual(link_result["stderr"], "path escapes workspace root")
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
        (home / "note.txt").write_text("home", encoding="utf-8")
        for folder in named.values():
            folder.mkdir()
            (folder / "note.txt").write_text("keep", encoding="utf-8")
        dot_skill = Path.home() / ".agents" / "skills" / "note.md"
        dot_key = Path.home() / ".agents" / "keys" / "secret.ed25519"
        verb = "python -c \"import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'), end='')\""
        with self._env(True, AGENTS_HOME=str(home), **{key: str(path) for key, path in named.items()}):
            def read(path: Path):
                return execute_job(self._job(verb, str(self.root)), extra_argv=[str(path)], log_path=self.log)

            home_file = read(home / "note.txt")
            self.assertEqual(home_file["status"], "SUCCESS", home_file["stderr"])
            self.assertEqual(home_file["stdout"], "home")
            for folder in named.values():
                result = read(folder / "note.txt")
                self.assertEqual(result["status"], "SUCCESS", result["stderr"])
                self.assertEqual(result["stdout"], "keep")
            cwd_result = execute_job(
                self._job("python -c \"print('ok')\"", str(home)),
                log_path=self.log,
            )
            self.assertEqual(cwd_result["status"], "SUCCESS", cwd_result["stderr"])
            from runner.executor import refuse_workspace

            self.assertIsNone(refuse_workspace(str(self.root), ["python", str(dot_skill)]))
            self.assertEqual(
                refuse_workspace(str(self.root), ["python", str(dot_key)]),
                "path escapes workspace root",
            )
            leaked = read(self.secret)
            self.assertEqual(leaked["status"], "ERROR")
            self.assertEqual(leaked["stderr"], "path escapes workspace root")

    def test_blank_unlocked(self) -> None:
        read_verb = "python -c \"import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'), end='')\""
        with self._env(False, AGENTS_WORKSPACE_DIR="   "):
            result = execute_job(
                self._job(read_verb, str(self.root)),
                extra_argv=[str(self.secret)],
                log_path=self.log,
            )
        self.assertEqual(result["status"], "SUCCESS", result["stderr"])
        self.assertEqual(result["stdout"], "TOPSECRET")

    def test_unset_unchanged(self) -> None:
        canary = self.outside / "canary.txt"
        verb = "python -c \"open('canary.txt','w',encoding='utf-8').write('x')\""
        read_verb = "python -c \"import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'), end='')\""
        with self._env(False):
            cwd_result = execute_job(self._job(verb, str(self.outside)), log_path=self.log)
            path_result = execute_job(
                self._job(read_verb, str(self.root)),
                extra_argv=[str(self.secret)],
                log_path=self.log,
            )
        self.assertEqual(cwd_result["status"], "SUCCESS", cwd_result["stderr"])
        self.assertEqual(canary.read_text(encoding="utf-8"), "x")
        self.assertEqual(path_result["status"], "SUCCESS", path_result["stderr"])
        self.assertEqual(path_result["stdout"], "TOPSECRET")


if __name__ == "__main__":
    unittest.main()
