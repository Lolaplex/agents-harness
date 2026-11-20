"""Session ops: /undo, /retry rewind, /compress frames over traces."""

import os
import tempfile
import unittest
from pathlib import Path

from runner import session_ops


class TestSessionOps(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.traces = Path(self._tmp.name) / "traces"
        os.environ["AGENTS_TRACES_DIR"] = str(self.traces)
        from agents_traces.store import TraceStore

        self.store = TraceStore(self.traces)
        self.session = "ses_ops1"

    def _say(self, role: str, text: str) -> None:
        from agents_traces.assemble import record_message

        record_message(self.session, role, text, store=self.store)

    def _projection(self) -> list[dict]:
        from agents_traces.assemble import assemble_messages

        return assemble_messages(self.session, store=self.store, limit=50)

    def test_undo_covers_last_turn(self):
        self._say("user", "u1")
        self._say("assistant", "a1")
        self._say("user", "u2")
        self._say("assistant", "a2")
        res = session_ops.undo_last_turn(self.session)
        self.assertTrue(res["ok"])
        msgs = [m["content"] for m in self._projection()]
        self.assertTrue(any("u1" in c for c in msgs))
        self.assertFalse(any("u2" in c or "a2" in c for c in msgs))

    def test_undo_empty_session(self):
        res = session_ops.undo_last_turn("ses_none")
        self.assertFalse(res["ok"])

    def test_rewind_keeps_last_user_message(self):
        self._say("user", "u1")
        self._say("assistant", "a1")
        res = session_ops.rewind_after_last_user(self.session)
        self.assertTrue(res["ok"])
        self.assertEqual(res["covered_events"], 1)
        msgs = self._projection()
        self.assertTrue(msgs)
        self.assertIn("u1", msgs[-1]["content"])
        self.assertFalse(any("a1" in m["content"] for m in msgs))

    def test_rewind_nothing_after_user(self):
        self._say("user", "u1")
        res = session_ops.rewind_after_last_user(self.session)
        self.assertTrue(res["ok"])
        self.assertEqual(res["covered_events"], 0)

    def test_compress_frame_covers_old_events(self):
        for i in range(10):
            self._say("user" if i % 2 == 0 else "assistant", f"m{i}")
        res = session_ops.compress_session(self.session, keep_last=3)
        self.assertTrue(res["ok"])
        msgs = self._projection()
        contents = [m["content"] for m in msgs]
        self.assertTrue(any("compacted" in c for c in contents))
        self.assertFalse(any("m0" in c for c in contents))
        self.assertTrue(any("m9" in c for c in contents))

    def test_compress_nothing_to_do(self):
        self._say("user", "u1")
        res = session_ops.compress_session(self.session, keep_last=6)
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
