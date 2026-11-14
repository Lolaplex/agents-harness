"""Search coverage: files searched, truncation, and a line outside the fence."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.loop import _run_tool_calls
from runner.search_report import search_notice


class TestSearchNotice(unittest.TestCase):
    def test_stderr_truncated_is_not_silent(self) -> None:
        notice = search_notice(
            {"name": "mcp.terminal"},
            {"command": "grep", "pattern": "frontend"},
            "",
            "truncated\n",
        )
        self.assertIn('files_searched="unknown"', notice)
        self.assertIn('truncated="1"', notice)
        self.assertIn('matches="0"', notice)

    def test_tool_count_wins_over_a_walk(self) -> None:
        stdout = json.dumps(
            {
                "files_searched": 9,
                "truncated": False,
                "matches": [{"path": "a.py", "line": 1, "text": "x"}, {"path": "b.py", "line": 2, "text": "y"}],
            }
        )
        notice = search_notice({"name": "mcp.terminal"}, {"command": "grep"}, stdout, "")
        self.assertIn('files_searched="9"', notice)
        self.assertIn('truncated="0"', notice)
        self.assertIn('matches="2"', notice)

    def test_finished_grep_counts_files_in_the_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("x", encoding="utf-8")
            (root / "README.md").write_text("y", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "skip.js").write_text("z", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp}, clear=False):
                notice = search_notice(
                    {"name": "mcp.terminal"},
                    {"command": "grep", "pattern": "missing"},
                    "",
                    "",
                )
        self.assertIn('files_searched="2"', notice)
        self.assertIn('truncated="0"', notice)
        self.assertIn('matches="0"', notice)

    def test_glob_shorter_than_the_tree_is_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x", encoding="utf-8")
            (root / "b.py").write_text("y", encoding="utf-8")
            (root / "c.txt").write_text("z", encoding="utf-8")
            with patch.dict(os.environ, {"AGENTS_WORKDIR": tmp}, clear=False):
                notice = search_notice(
                    {"name": "mcp.terminal"},
                    {"command": "glob", "pattern": "*.py"},
                    "a.py\n",
                    "",
                )
        self.assertIn('files_searched="2"', notice)
        self.assertIn('truncated="1"', notice)
        self.assertIn('matches="1"', notice)

    def test_non_search_has_no_line(self) -> None:
        self.assertEqual(search_notice({"name": "mcp.calendar.list"}, {}, "[]", ""), "")

    def test_loop_puts_the_line_after_the_fence(self) -> None:
        rec = {
            "status": "SUCCESS",
            "exit_code": 0,
            "stdout": "",
            "stderr": "truncated\n",
            "stdout_tail": "",
            "stderr_tail": "truncated\n",
        }
        call = {
            "id": "g1",
            "type": "function",
            "function": {
                "name": "call_job",
                "arguments": json.dumps(
                    {"name": "mcp.terminal", "arguments": {"command": "grep", "pattern": "frontend"}}
                ),
            },
        }
        with patch("runner.cordis_tools.execute_job", return_value=rec):
            messages = _run_tool_calls([call], session="ses_search")
        content = messages[0]["content"]
        self.assertLess(content.rindex("</untrusted_data>"), content.index("<search "))
        self.assertIn('truncated="1"', content)
        self.assertIn('files_searched="unknown"', content)


if __name__ == "__main__":
    unittest.main()
