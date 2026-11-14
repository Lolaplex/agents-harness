"""Content retrieval: fake backend, harness clips, and no-backend fallback."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runner.cordis_tools import handle_cordis_tool
from runner.retrieve import (
    content_sha256,
    read_content,
    remember_content,
    reset_content_state,
    tool_history_block,
)


def _call(name: str, arguments: dict) -> str:
    return handle_cordis_tool(
        "call_job",
        {"function": {"arguments": json.dumps({"name": name, "arguments": arguments})}},
    )


class TestRetrieve(unittest.TestCase):
    def setUp(self) -> None:
        reset_content_state()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"AGENTS_CONTENT_DIR": self._tmpdir.name}, clear=False)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        reset_content_state()
        self._tmpdir.cleanup()

    def test_fake_backend_pages_a_body(self) -> None:
        body = "ALPHA" + ("x" * 50) + "OMEGA"
        sha = content_sha256(body)
        seen: list[tuple] = []

        def backend(session_id, digest, offset=0, limit=8000):
            seen.append((session_id, digest, offset, limit))
            self.assertIn(digest, (sha, sha[:12]))
            window = None if limit is None or limit < 0 else limit
            piece = body[offset:] if window is None else body[offset : offset + window]
            return {
                "text": piece,
                "digest": sha,
                "total_chars": len(body),
                "truncated": offset + len(piece) < len(body) or offset != 0,
                "offset": offset,
            }

        from runner import register_content_backend

        register_content_backend(backend)
        raw = _call(
            "read_content",
            {"digest": sha[:12], "session_id": "ses_demo", "offset": 0, "limit": 5},
        )
        page = json.loads(raw)
        self.assertEqual(page["text"], "ALPHA")
        self.assertEqual(page["digest"], sha)
        self.assertEqual(page["total_chars"], len(body))
        self.assertTrue(page["truncated"])
        self.assertEqual(seen[0][0], "ses_demo")
        self.assertEqual(seen[0][2], 0)
        self.assertEqual(seen[0][3], 5)

        nxt = json.loads(_call("read_content", {"digest": sha, "offset": 5, "limit": 50, "session_id": "ses_demo"}))
        self.assertEqual(nxt["text"], "x" * 50)
        self.assertTrue(nxt["truncated"])
        tail = json.loads(
            _call("read_content", {"digest": sha[:12], "offset": 5 + 50, "limit": 8000, "session_id": "ses_demo"})
        )
        self.assertEqual(tail["text"], "OMEGA")
        self.assertTrue(tail["truncated"])
        self.assertEqual(5 + 50 + len(tail["text"]), tail["total_chars"])

    def test_content_slice_shape_is_accepted(self) -> None:
        body = "slice-body"
        sha = content_sha256(body)
        page = SimpleNamespace(
            session="ses_shape",
            digest=sha,
            digest_short=sha[:12],
            text=body,
            offset=0,
            limit=8000,
            total_chars=len(body),
            truncated=False,
            stored_truncated=False,
        )

        def backend(session_id, digest, offset=0, limit=8000):
            self.assertEqual(session_id, "ses_shape")
            return page

        from runner import register_content_backend

        register_content_backend(backend)
        raw = _call("read_content", {"digest": sha[:12], "session_id": "ses_shape", "limit": 8000})
        parsed = json.loads(raw)
        self.assertEqual(parsed["text"], body)
        self.assertEqual(parsed["digest"], sha)
        self.assertEqual(parsed["total_chars"], len(body))
        self.assertFalse(parsed["truncated"])
        self.assertFalse(parsed["stored_truncated"])

    def test_no_backend_does_not_invent_text(self) -> None:
        secret = "INVENTED-BODY-SHOULD-NOT-APPEAR"
        raw = _call("read_content", {"digest": "abc123abc123", "session_id": "ses_missing"})
        self.assertIn("Content unavailable", raw)
        self.assertIn("Do not invent the missing text.", raw)
        self.assertNotIn(secret, raw)
        self.assertNotIn("<untrusted_data", raw)
        direct = read_content("ses_missing", "abc123abc123")
        self.assertFalse(direct["available"])
        self.assertNotIn("text", direct)

    def test_backend_not_found_is_unavailable(self) -> None:
        def backend(session_id, digest, offset=0, limit=8000):
            raise RuntimeError("boom-detail")

        from runner import register_content_backend

        register_content_backend(backend)
        raw = _call("read_content", {"digest": "def123def123"})
        self.assertIn("Content unavailable", raw)
        self.assertIn("content_unavailable", raw)
        self.assertNotIn("boom-detail", raw)

    def test_history_keeps_more_than_three_and_a_ref(self) -> None:
        events = []
        fulls = []
        for i in range(5):
            body = f"EVENT{i}-" + ("m" * 500) + f"-END{i}"
            fulls.append(body)
            events.append(SimpleNamespace(tool="call_job", args={"name": f"mod.{i}"}, result=body, metadata={}))
        with patch.dict(os.environ, {"AGENTS_HISTORY_TOOL_PREVIEW": "400"}, clear=False):
            block = tool_history_block(events, session_id="ses_hist")
        self.assertIn("mod.0", block)
        self.assertIn("mod.4", block)
        self.assertEqual(block.count("<page "), 5)
        digest = block.split('digest="', 1)[1].split('"', 1)[0]
        page = read_content("ses_hist", digest, offset=0, limit=-1)
        self.assertEqual(page["text"], fulls[0])
        self.assertFalse(page["truncated"])

    def test_preserved_trace_ref_is_not_replaced(self) -> None:
        sha = "ab" * 32
        prefix = "only-a-prefix"
        ref = (
            f'<ref digest="{sha[:12]}" sha256="{sha}" chars="99999" truncated="1" '
            'session="ses_t"/>'
        )
        event = SimpleNamespace(
            tool="call_job",
            args={"name": "mcp.traces"},
            result=prefix + "\n" + ref,
            metadata={"result_truncated": True, "result_sha256": sha, "result_chars": 99999},
        )
        block = tool_history_block([event], session_id="ses_t")
        self.assertIn(f'sha256="{sha}"', block)
        self.assertEqual(block.count("<ref "), 1)
        missing = read_content("ses_t", sha[:12])
        self.assertFalse(missing["available"])

    def test_broken_content_read_cmd_is_unavailable(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENTS_CONTENT_READ_CMD": 'python "unterminated'},
            clear=False,
        ):
            page = read_content("ses_bad_cmd", "abc123abc123")
        self.assertFalse(page["available"])
        self.assertEqual(page["error"], "content_unavailable")
        self.assertNotIn("text", page)

    def test_command_backend(self) -> None:
        body = "from-cmd"
        sha = hashlib.sha256(body.encode()).hexdigest()
        script = Path(self._tmpdir.name) / "read.py"
        script.write_text(
            "import json,sys\n"
            "req=json.load(sys.stdin)\n"
            f"body={body!r}\n"
            "off=int(req.get('offset') or 0)\n"
            "limit=req.get('limit')\n"
            "piece=body[off:] if limit is None or int(limit)<0 else body[off:off+int(limit)]\n"
            "json.dump({'text':piece,'digest':"
            f"{sha!r},'total_chars':len(body),"
            "'truncated': off+len(piece)<len(body) or off!=0}, sys.stdout)\n",
            encoding="utf-8",
        )
        import sys

        cmd = f"{sys.executable} {script}"
        with patch.dict(os.environ, {"AGENTS_CONTENT_READ_CMD": cmd}, clear=False):
            page = read_content("ses_cmd", sha[:12], offset=0, limit=4)
        self.assertTrue(page["available"])
        self.assertEqual(page["text"], "from")
        self.assertEqual(page["digest"], sha)
        self.assertEqual(page["total_chars"], len(body))
        self.assertTrue(page["truncated"])


@unittest.skipUnless(importlib.util.find_spec("agents_traces"), "agents-traces not installed")
class TestTracesReadContentShape(unittest.TestCase):
    """Optional. The harness backend is a lambda; production code does not import traces."""

    def setUp(self) -> None:
        reset_content_state()

    def tearDown(self) -> None:
        reset_content_state()

    def test_real_read_content_round_trip(self) -> None:
        from agents_traces.assemble import record_message
        from agents_traces.content import content_sha256 as traces_sha
        from agents_traces.content import read_content as traces_read

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENTS_TRACES_DIR": tmp}, clear=False):
                body = "TRACE" + ("n" * 30) + "TAIL"
                record_message("ses_real", "user", body, channel="local", user="probe")
                sha = traces_sha(body)

                def backend(session_id, digest, offset=0, limit=8000):
                    piece = traces_read(session_id, digest, offset=offset, limit=limit)
                    return piece

                from runner import register_content_backend

                register_content_backend(backend)
                raw = _call(
                    "read_content",
                    {"digest": sha[:12], "session_id": "ses_real", "offset": 5, "limit": 30},
                )
        page = json.loads(raw)
        self.assertEqual(page["text"], "n" * 30)
        self.assertEqual(page["digest"], sha)
        self.assertEqual(page["total_chars"], len(body))
        self.assertTrue(page["truncated"])
        self.assertIn("stored_truncated", page)


class TestRemember(unittest.TestCase):
    def tearDown(self) -> None:
        reset_content_state()

    def test_ref_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENTS_CONTENT_DIR": tmp}, clear=False):
                reset_content_state()
                ref = remember_content("hello", session_id="ses_r")
                self.assertIn('truncated="1"', ref)
                digest = ref.split('digest="', 1)[1].split('"', 1)[0]
                page = read_content("ses_r", digest, offset=1, limit=3)
                self.assertEqual(page["text"], "ell")
                self.assertEqual(page["total_chars"], 5)
                self.assertTrue(page["truncated"])


if __name__ == "__main__":
    unittest.main()
