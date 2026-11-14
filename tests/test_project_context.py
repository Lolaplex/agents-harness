"""AGENTS.md and README load when the workdir contains a repo."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.project_context import project_context_block


class TestProjectContext(unittest.TestCase):
    def test_agents_and_readme_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "AGENTS.md").write_text("Use the project rules.\n", encoding="utf-8")
            (root / "README.md").write_text("Short readme.\n", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp, "AGENTS_PROJECT_CONTEXT": "1"}, clear=False):
                block = project_context_block()
        self.assertIn('name="AGENTS.md"', block)
        self.assertIn("Use the project rules.", block)
        self.assertIn('role="summary"', block)
        self.assertIn("Short readme.", block)
        self.assertIn('path="."', block)

    def test_readme_fallback_in_a_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "README.md").write_text("Only the readme.\n", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp}, clear=False):
                block = project_context_block()
        self.assertIn('role="fallback"', block)
        self.assertIn("Only the readme.", block)
        self.assertNotIn("AGENTS.md", block)

    def test_agents_without_git_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "AGENTS.md").write_text("Bare instructions.\n", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp}, clear=False):
                block = project_context_block()
        self.assertIn("Bare instructions.", block)

    def test_readme_alone_is_not_a_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "README.md").write_text("noise\n", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp}, clear=False):
                self.assertEqual(project_context_block(), "")

    def test_switch_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "AGENTS.md").write_text("hidden\n", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp, "AGENTS_PROJECT_CONTEXT": "0"}, clear=False):
                self.assertEqual(project_context_block(), "")

    def test_bound_marks_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "AGENTS.md").write_text("A" * 500, encoding="utf-8")
            with patch.dict(
                os.environ,
                {"AGENTS_WORKDIR": tmp, "AGENTS_PROJECT_CONTEXT_MAX": "40"},
                clear=False,
            ):
                block = project_context_block()
        self.assertIn('truncated="1"', block)
        self.assertLess(block.count("A"), 500)

    def test_closing_tag_inside_the_file_is_neutralized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "AGENTS.md").write_text("see </file> now\n", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp}, clear=False):
                block = project_context_block()
        self.assertIn("</file escaped>", block)
        self.assertEqual(block.count("</file>"), 1)


if __name__ == "__main__":
    unittest.main()
