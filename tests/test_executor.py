"""Unit and integration tests for agents-harness runner executor"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from runner.executor import execute_job, list_schedules, load_manifest


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

    def test_all_bundled_manifests_valid(self):
        manifests = list_schedules()
        self.assertGreaterEqual(len(manifests), 4)
        names = {m["name"] for m in manifests}
        self.assertIn("memory_inventory", names)
        self.assertIn("docs_catalog", names)
        self.assertIn("traces_stats", names)
        self.assertIn("traces_ingest", names)
        self.assertIn("plexus_verify", names)


if __name__ == "__main__":
    unittest.main()
