"""Tests for rich approval payload previews (U02, agents-harness#44)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runner.approval import gate_module, _build_preview


class TestApprovalPreview(unittest.TestCase):
    def test_write_and_edit_diff_previews(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            ws = SimpleNamespace(workdir=str(wd))
            target = wd / "notes.txt"
            target.write_text("line 1\nline 2\n", encoding="utf-8", newline="\n")

            p_write = _build_preview(
                "mcp.terminal",
                {"argv": ["write", "notes.txt", "--content", "line 1\nline 2 updated\n"]},
                workspace=ws,
            )
            self.assertIsNotNone(p_write)
            assert p_write is not None
            self.assertEqual(p_write["kind"], "diff")
            self.assertEqual(p_write["path"], "notes.txt")
            self.assertEqual(p_write["old_text"], "line 1\nline 2\n")
            self.assertEqual(p_write["new_text"], "line 1\nline 2 updated\n")
            self.assertIn("-line 2", p_write["unified"])
            self.assertIn("+line 2 updated", p_write["unified"])

            p_edit = _build_preview(
                "mcp.terminal.edit",
                {"path": "notes.txt", "old": "line 2", "new": "line TWO"},
                workspace=ws,
            )
            assert p_edit is not None
            self.assertEqual(p_edit["kind"], "diff")
            self.assertIn("+line TWO", p_edit["unified"])

    def test_path_escape_and_symlink_never_read_outside_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wd = root / "work"
            wd.mkdir()
            secret = root / "host_secret.txt"
            secret.write_text("TOP_SECRET_DO_NOT_LEAK", encoding="utf-8")
            ws = SimpleNamespace(workdir=str(wd))

            for escape in ("../host_secret.txt", str(secret)):
                prev = _build_preview(
                    "mcp.terminal",
                    {"command": "write", "path": escape, "content": "x"},
                    workspace=ws,
                )
                assert prev is not None
                self.assertEqual(prev.get("error"), "path escapes workdir")
                self.assertNotIn("TOP_SECRET_DO_NOT_LEAK", json.dumps(prev))

    def test_large_file_preview_caps_within_64kb(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            big = wd / "big.txt"
            big.write_text("A" * 80_000, encoding="utf-8")
            ws = SimpleNamespace(workdir=str(wd))

            prev = _build_preview(
                "mcp.terminal",
                {"command": "write", "path": "big.txt", "content": "B" * 80_000},
                workspace=ws,
            )
            assert prev is not None
            self.assertTrue(prev["truncated"])
            self.assertLessEqual(len(json.dumps(prev, ensure_ascii=False).encode("utf-8")), 64_000)

    def test_command_preview_risk_classification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = SimpleNamespace(workdir=td)
            low = _build_preview("mcp.terminal", {"argv": ["run", "--", "git", "status"]}, workspace=ws)
            med = _build_preview("mcp.terminal", {"argv": ["run", "--", "pip", "install", "foo"]}, workspace=ws)
            high = _build_preview("mcp.terminal", {"argv": ["run", "--", "rm", "-rf", "dist"]}, workspace=ws)
            push = _build_preview("mcp.terminal", {"argv": ["run", "--", "git", "push", "origin", "main"]}, workspace=ws)
            assert low and med and high and push
            self.assertEqual(low["risk"], "low")
            self.assertEqual(med["risk"], "medium")
            self.assertEqual(high["risk"], "high")
            self.assertEqual(push["risk"], "high")
            self.assertEqual(low["argv"], ["git", "status"])
            self.assertEqual(Path(low["cwd"]).resolve(), Path(td).resolve())

    def test_gate_module_attaches_preview_to_approval_cmd_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wd = root / "work"
            wd.mkdir()
            captured = root / "captured.json"
            helper = root / "helper.py"
            helper.write_text(
                f"import sys; open({str(captured)!r}, 'w', encoding='utf-8').write(sys.stdin.read())",
                encoding="utf-8",
            )
            ws = SimpleNamespace(workdir=str(wd), host=None, dir=root / "ws")
            with patch.dict(
                os.environ,
                {
                    "AGENTS_HOST_DIR": str(root / "host"),
                    "AGENTS_APPROVAL_CMD": f'"{sys.executable}" "{helper}"',
                    "AGENTS_APPROVAL_MODE": "ask",
                },
            ):
                allowed, _ = gate_module(
                    {"name": "mcp.terminal", "mutates": True},
                    {"argv": ["write", "new.txt", "--content", "hello world"]},
                    workspace=ws,
                )
            self.assertTrue(allowed)
            sent = json.loads(captured.read_text(encoding="utf-8"))
            self.assertIn("preview", sent)
            self.assertEqual(sent["preview"]["kind"], "diff")
            self.assertEqual(sent["preview"]["path"], "new.txt")
            self.assertEqual(sent["preview"]["new_text"], "hello world")
