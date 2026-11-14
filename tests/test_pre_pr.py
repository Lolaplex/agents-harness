"""Pre-PR discovery from project files, and running only those commands."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.cordis_tools import handle_cordis_tool
from runner.pre_pr import pre_pr_report


def _call(arguments: dict) -> str:
    return handle_cordis_tool(
        "call_job",
        {"function": {"arguments": json.dumps({"name": "repo.pre_pr", "arguments": arguments})}},
    )


class TestPrePr(unittest.TestCase):
    def test_package_json_and_pyproject_are_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pnpm-lock.yaml").write_text("", encoding="utf-8")
            (root / "package.json").write_text(
                json.dumps({"scripts": {"test": "vitest", "build": "tsc", "dev": "vite"}}),
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts='-q'\n", encoding="utf-8")
            (root / "Makefile").write_text("test:\n\ttrue\n\n.PHONY: test\n", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp}, clear=False):
                text, ok = pre_pr_report({})
        self.assertTrue(ok)
        report = json.loads(text)
        argv = [tuple(row["argv"]) for row in report["commands"]]
        self.assertIn(("pnpm", "run", "test"), argv)
        self.assertIn(("pnpm", "run", "build"), argv)
        self.assertIn((sys.executable, "-m", "pytest"), argv)
        self.assertIn(("make", "test"), argv)
        self.assertNotIn(("pnpm", "run", "dev"), argv)
        self.assertFalse(report["ran"])

    def test_agents_md_command_runs_and_shell_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            exe = sys.executable
            (root / "AGENTS.md").write_text(
                "# Tests\n\n```bash\n"
                f'{exe} -c "import unittest; print(\'ok\')"\n'
                f"{exe} -m unittest discover tests && echo hi\n"
                "```\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp}, clear=False):
                listed, ok = pre_pr_report({})
                ran_text, ran_ok = pre_pr_report({"run": True})
        self.assertTrue(ok)
        listed_report = json.loads(listed)
        unsafe = [row for row in listed_report["commands"] if row["runnable"] is False]
        self.assertEqual(len(unsafe), 1)
        self.assertTrue(ran_ok)
        report = json.loads(ran_text)
        self.assertTrue(report["ran"])
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["results"][0]["exit_code"], 0)
        self.assertIn("ok", report["results"][0]["stdout"])
        self.assertTrue(report["ok"])

    def test_path_escape_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp}, clear=False):
                text, ok = pre_pr_report({"path": "/etc"})
        self.assertFalse(ok)
        self.assertEqual(json.loads(text)["error"], "path escapes workspace root")

    def test_several_projects_are_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("web", "api"):
                repo = root / name
                repo.mkdir()
                (repo / ".git").mkdir()
                (repo / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp}, clear=False):
                text, ok = pre_pr_report({})
        self.assertFalse(ok)
        report = json.loads(text)
        self.assertEqual(report["error"], "multiple projects")
        self.assertEqual({item["path"] for item in report["projects"]}, {"web", "api"})

    def test_call_job_lists_without_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Makefile").write_text("lint:\n\ttrue\n", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp}, clear=False):
                raw = _call({})
        report = json.loads(raw)
        self.assertEqual(report["commands"][0]["argv"], ["make", "lint"])
        self.assertFalse(report["ran"])

    def test_empty_project_invents_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp}, clear=False):
                text, ok = pre_pr_report({"run": True})
        self.assertTrue(ok)
        report = json.loads(text)
        self.assertEqual(report["commands"], [])
        self.assertNotIn("results", report)


if __name__ == "__main__":
    unittest.main()
