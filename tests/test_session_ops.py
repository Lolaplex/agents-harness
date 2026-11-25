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


class TestProjectionHook(unittest.TestCase):
    """session_ops mirrors coverage frames into <ws>/store.db through the
    soft-imported klanker.msgsync projection path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        host = Path(self._tmp.name) / "host"
        os.environ.pop("AGENTS_TRACES_DIR", None)
        os.environ["AGENTS_HOST_DIR"] = str(host)
        os.environ["AGENTS_WORKSPACE"] = "w1"
        from agents_workspace import Host

        self.ws = Host.from_env().create("w1")
        from agents_traces.store import TraceStore

        self.store = TraceStore(self.ws.traces)
        self.session = "ses_proj1"

    def tearDown(self):
        os.environ.pop("AGENTS_HOST_DIR", None)
        os.environ.pop("AGENTS_WORKSPACE", None)

    def _say(self, role: str, text: str) -> None:
        from agents_traces.assemble import record_message

        record_message(self.session, role, text, store=self.store)

    def _spy_modules(self):
        """Fake the whole soft-import chain — klanker and agents-store
        are absent in standalone CI, so both parent packages and the
        msgsync/db submodules live in sys.modules for the import to
        resolve at all."""
        import types

        calls = []
        conn = types.SimpleNamespace(close=lambda: None)

        fake_db = types.ModuleType("agents_store.db")
        fake_db.connect = lambda *a, **kw: conn
        fake_store = types.ModuleType("agents_store")
        fake_store.db = fake_db

        fake_ms = types.ModuleType("klanker.msgsync")
        fake_ms.ensure_domain = lambda conn: None
        fake_ms.local_node_id = lambda: "testhost"
        fake_ms.project_traces = (
            lambda conn, ws_dir, **kw: calls.append(("traces", ws_dir, kw))
            or {"projected": 0, "skipped": 0, "files": 0})
        fake_ms.project_event = (
            lambda conn, node, ev, **kw: calls.append(("event", node, ev, kw))
            or "projected")
        fake_klanker = types.ModuleType("klanker")
        fake_klanker.msgsync = fake_ms

        modules = {
            "agents_store": fake_store,
            "agents_store.db": fake_db,
            "klanker": fake_klanker,
            "klanker.msgsync": fake_ms,
        }
        return modules, calls

    def test_undo_frame_reaches_msgsync(self):
        import sys
        from unittest.mock import patch

        modules, calls = self._spy_modules()
        self._say("user", "u1")
        self._say("assistant", "a1")
        self._say("user", "u2")
        with patch.dict(sys.modules, modules):
            res = session_ops.undo_last_turn(self.session)
        self.assertTrue(res["ok"])
        # project_traces must run BEFORE project_event: the tombstone
        # span only retires msg entities already in store.db.
        self.assertEqual(len(calls), 2)
        kind, ws_dir, kw = calls[0]
        self.assertEqual(kind, "traces")
        self.assertEqual(ws_dir, self.ws.dir)
        self.assertEqual(kw["traces_dir"], self.ws.traces)
        kind, node, ev, kw = calls[1]
        self.assertEqual(kind, "event")
        self.assertEqual(node, "testhost")
        self.assertEqual(ev.type, "frame")
        self.assertEqual(ev.metadata["kind"], "undo")
        self.assertEqual(kw["traces_dir"], self.ws.traces)

    def test_retry_frame_reaches_msgsync(self):
        import sys
        from unittest.mock import patch

        modules, calls = self._spy_modules()
        self._say("user", "u1")
        self._say("assistant", "a1")
        with patch.dict(sys.modules, modules):
            res = session_ops.rewind_after_last_user(self.session)
        self.assertTrue(res["ok"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "traces")
        self.assertEqual(calls[1][0], "event")
        self.assertEqual(calls[1][2].metadata["kind"], "retry")

    def test_compress_frame_reaches_msgsync(self):
        import sys
        from unittest.mock import patch

        modules, calls = self._spy_modules()
        for i in range(8):
            self._say("user" if i % 2 == 0 else "assistant", f"m{i}")
        with patch.dict(sys.modules, modules):
            res = session_ops.compress_session(self.session, keep_last=3)
        self.assertTrue(res["ok"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "traces")
        self.assertEqual(calls[1][0], "event")
        self.assertEqual(calls[1][2].type, "frame")
        rng = calls[1][2].metadata["summarized_from"]
        self.assertTrue(rng["first_ts"] and rng["last_ts"])

    def test_missing_klanker_is_silent_noop(self):
        import sys
        from unittest.mock import patch

        self._say("user", "u1")
        self._say("assistant", "a1")
        self._say("user", "u2")
        absent = {
            "agents_store": None,
            "agents_store.db": None,
            "klanker": None,
            "klanker.msgsync": None,
        }
        with patch.dict(sys.modules, absent):
            res = session_ops.undo_last_turn(self.session)
        self.assertTrue(res["ok"])
        frames = [
            e for e in self.store.iter_events(session_id=self.session)
            if e.type == "frame"
        ]
        self.assertEqual(len(frames), 1)


if __name__ == "__main__":
    unittest.main()
