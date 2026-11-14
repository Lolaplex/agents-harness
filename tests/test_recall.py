"""Pre-turn recall is off until a workspace or the environment enables it."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from agents_workspace import Host

from runner.loop import build_payload
from runner.recall import (
    RECALL_PROMPT_LINE,
    RecallHit,
    RecallRequest,
    clear_recall_providers,
    collect_hits,
    recall_enabled,
    recall_message,
    register_recall_provider,
    search_repo,
)


def _env(**extra: str) -> dict[str, str]:
    drop = {
        "AGENTS_RECALL",
        "AGENTS_RECALL_CMD",
        "AGENTS_RECALL_SOURCES",
        "AGENTS_RECALL_ROOT",
        "AGENTS_RECALL_MAX_CHARS",
        "AGENTS_GIT_REPO",
        "AGENTS_HOST_DIR",
        "AGENTS_WORKSPACE",
    }
    env = {key: value for key, value in os.environ.items() if key not in drop}
    env.update(extra)
    return env


class RecallSwitch(unittest.TestCase):
    def tearDown(self):
        clear_recall_providers()

    def test_default_is_off_and_adds_no_prompt_line(self):
        with patch.dict(os.environ, _env(), clear=True):
            self.assertFalse(recall_enabled())
            payload = build_payload("ses_recall", "What color is the sandbox canary?")
        blob = "\n".join(
            item["content"] for item in payload if isinstance(item.get("content"), str)
        )
        self.assertNotIn(RECALL_PROMPT_LINE, blob)
        self.assertNotIn('source="recall"', blob)

    def test_enabled_injects_one_line_and_bounded_hits(self):
        def provider(request: RecallRequest):
            self.assertIn("canary", request.query)
            return [
                RecallHit(source="memory", title="canary", text="The sandbox canary is ochre."),
                RecallHit(source="memory", title="pad", text="x" * 5000),
            ]

        register_recall_provider(provider)
        with patch.dict(
            os.environ,
            _env(AGENTS_RECALL="1", AGENTS_RECALL_SOURCES="registered", AGENTS_RECALL_MAX_CHARS="500"),
            clear=True,
        ):
            payload = build_payload("ses_recall", "What color is the sandbox canary?")
        blob = "\n".join(
            item["content"] for item in payload if isinstance(item.get("content"), str)
        )
        self.assertEqual(blob.count(RECALL_PROMPT_LINE), 1)
        notes = [item["content"] for item in payload if 'source="recall"' in str(item.get("content"))]
        self.assertEqual(len(notes), 1)
        self.assertIn("ochre", notes[0])
        self.assertIn("<untrusted_data", notes[0])
        self.assertLess(len(notes[0]), 800)
        self.assertEqual(payload[-1]["content"], "What color is the sandbox canary?")

    def test_provider_failure_is_skipped(self):
        def broken(_request: RecallRequest):
            raise RuntimeError("down")

        def healthy(_request: RecallRequest):
            return {"source": "memory", "text": "port 8741"}

        register_recall_provider(broken)
        register_recall_provider(healthy)
        with patch.dict(
            os.environ,
            _env(AGENTS_RECALL="1", AGENTS_RECALL_SOURCES="registered"),
            clear=True,
        ):
            text = recall_message("Which port does the relay listen on?")
        self.assertIn("8741", text)

    def test_off_switch_beats_workspace_enabled(self):
        with TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            host.dir.mkdir()
            ws = host.create("demo")
            ws.config.write_text(
                json.dumps({"recall": {"enabled": True}}),
                encoding="utf-8",
            )
            called = {"n": 0}

            def provider(_request: RecallRequest):
                called["n"] += 1
                return [RecallHit(source="memory", text="ochre")]

            register_recall_provider(provider)
            with patch.dict(os.environ, _env(AGENTS_RECALL="0"), clear=True):
                self.assertFalse(recall_enabled(ws))
                self.assertEqual(collect_hits("canary", workspace=ws), [])
            self.assertEqual(called["n"], 0)
            with patch.dict(os.environ, _env(), clear=True):
                self.assertTrue(recall_enabled(ws))

    def test_command_provider_reads_json_hits(self):
        script = (
            "import json,sys\n"
            "json.load(sys.stdin)\n"
            "print(json.dumps({'hits':[{'source':'memory','title':'canary','text':'ochre'}]}))\n"
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "recall.py"
            path.write_text(script, encoding="utf-8")
            cmd = " ".join([shlex.quote(sys.executable), shlex.quote(str(path))])
            with patch.dict(
                os.environ,
                _env(AGENTS_RECALL="1", AGENTS_RECALL_SOURCES="command", AGENTS_RECALL_CMD=cmd),
                clear=True,
            ):
                hits = collect_hits("canary color")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].text, "ochre")
        self.assertEqual(hits[0].source, "memory")

    def test_repo_scan_is_bounded_and_skips_git(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text(
                "The sandbox canary is ochre.\n", encoding="utf-8"
            )
            git_dir = root / ".git"
            git_dir.mkdir()
            (git_dir / "hidden.md").write_text("secret ochre token\n", encoding="utf-8")
            hits = search_repo(RecallRequest(query="sandbox canary color", limit=5), root)
        self.assertTrue(hits)
        self.assertIn("ochre", hits[0].text)
        self.assertNotIn("secret", hits[0].text)
        self.assertEqual(hits[0].source, "repo")
