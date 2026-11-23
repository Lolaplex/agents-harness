"""persona.* Cordis verbs: inbox messaging, hop guard, unspoofable sender."""

import json
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from agents_workspace import Host

from runner import persona_tool


def _run(argv, env=None):
    import io
    from contextlib import redirect_stderr, redirect_stdout

    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, env or {}):
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = persona_tool.main(argv)
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
    return rc, out.getvalue(), err.getvalue()


class TestPersonaTool(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.host = Host(dir=Path(self._tmp.name) / "host")
        self.ws = self.host.create("ws1", persona="default")
        for pid in ("cos", "ops"):
            pdir = self.ws.personas / pid
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "persona.json").write_text(
                json.dumps({"id": pid}), encoding="utf-8"
            )
        self.env = {
            "AGENTS_HOST_DIR": str(self.host.dir),
            "AGENTS_WORKSPACE": "ws1",
            "AGENTS_PERSONA": "default",
        }

    def test_list_shows_peer_policy(self):
        (self.ws.personas / "cos" / "persona.json").write_text(
            json.dumps({"id": "cos", "accepts_messages": False, "role": "ops"}),
            encoding="utf-8",
        )
        rc, out, _ = _run(["list"], self.env)
        self.assertEqual(rc, 0)
        rows = {r["id"]: r for r in json.loads(out)["personas"]}
        self.assertIn("cos", rows)
        self.assertFalse(rows["cos"]["accepts_messages"])
        self.assertEqual(rows["cos"]["role"], "ops")

    def test_handoff_creates_task_with_env_sender(self):
        rc, out, err = _run(
            ["handoff", "--to", "cos", "--title", "pick this up", "--note", "ctx"],
            self.env,
        )
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        from agents_workspace.inbox import find_task, list_inbox

        owner, task = find_task(self.ws, data["task_id"])
        self.assertEqual(owner, "cos")
        self.assertEqual(task["from_persona"], "default")
        self.assertEqual(task["mode"], "handoff")
        self.assertTrue(task["thread_id"].startswith("thr_"))
        self.assertEqual(len(list_inbox(self.ws, "cos")), 1)

    def test_sender_not_spoofable_from_args(self):
        # no --from arg exists; AGENTS_PERSONA is the only sender source
        rc, out, err = _run(
            ["handoff", "--to", "cos", "--title", "x", "--from", "ops"], self.env
        )
        self.assertNotEqual(rc, 0)  # argparse rejects the unknown flag

    def test_hop_guard_refuses_at_max(self):
        env = dict(self.env, AGENTS_PERSONA_HOP="3")
        rc, _, err = _run(["handoff", "--to", "cos", "--title", "x"], env)
        self.assertNotEqual(rc, 0)
        self.assertIn("hop limit", err)
        rc, _, err = _run(["ask", "cos", "q"], env)
        self.assertNotEqual(rc, 0)

    def test_self_loop_refused(self):
        rc, _, err = _run(["ask", "default", "hello?"], self.env)
        self.assertNotEqual(rc, 0)
        self.assertIn("self-message", err)
        rc, _, _ = _run(["handoff", "--to", "default", "--title", "x"], self.env)
        self.assertNotEqual(rc, 0)

    def test_inbox_lists_tasks_and_status_filter(self):
        _run(["handoff", "--to", "default", "--title", "t1"], dict(self.env, AGENTS_PERSONA_SELF="1"))
        rc, out, _ = _run(["inbox"], self.env)
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["persona"], "default")
        self.assertEqual(len(data["tasks"]), 1)
        rc, out, _ = _run(["inbox", "--status", "done"], self.env)
        self.assertEqual(json.loads(out)["tasks"], [])

    def test_reply_roundtrip(self):
        _, out, _ = _run(
            ["handoff", "--to", "cos", "--title", "q"], self.env
        )
        tid = json.loads(out)["task_id"]
        rc, out, _ = _run(
            ["reply", tid, "--result", "done it", "--status", "done"], self.env
        )
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["owner"], "default")  # returned to sender
        self.assertEqual(data["status"], "done")

    def test_ask_bind_failure_orphans_no_task(self):
        # The parent-session bind runs BEFORE create_task — a declined
        # bind must leave zero inbox state behind.
        from agents_workspace.inbox import list_inbox

        from runner import session_gate

        session_gate._reset_for_tests()  # _fired latches once per process
        session_gate.register_session_gate(lambda *a: False)
        env = dict(self.env, AGENTS_PARENT_SESSION="ses_root")
        try:
            rc, _, err = _run(["ask", "cos", "hello"], env)
        finally:
            session_gate._reset_for_tests()
        self.assertNotEqual(rc, 0)
        self.assertIn("session bind declined", err)
        self.assertEqual(list_inbox(self.ws, "cos"), [])

    def test_handoff_wake_bind_failure_creates_no_task(self):
        from agents_workspace.inbox import list_inbox

        from runner import session_gate

        session_gate._reset_for_tests()  # _fired latches once per process
        session_gate.register_session_gate(lambda *a: False)
        env = dict(self.env, AGENTS_PARENT_SESSION="ses_root")
        try:
            rc, _, err = _run(
                ["handoff", "--to", "cos", "--title", "x", "--wake"], env
            )
        finally:
            session_gate._reset_for_tests()
        self.assertNotEqual(rc, 0)
        self.assertIn("session bind declined", err)
        self.assertEqual(list_inbox(self.ws, "cos"), [])

    def test_help_json(self):
        rc, out, _ = _run(["--help-json"], self.env)
        spec = json.loads(out)
        self.assertEqual(spec["commands"]["list"]["side_effects"], "read-only")
        self.assertEqual(spec["commands"]["ask"]["side_effects"], "mutating")

    def test_peer_policy_refusal_exit(self):
        (self.ws.personas / "cos" / "persona.json").write_text(
            json.dumps({"id": "cos", "accepts_messages": False}), encoding="utf-8"
        )
        rc, _, err = _run(["handoff", "--to", "cos", "--title", "x"], self.env)
        self.assertNotEqual(rc, 0)
        self.assertIn("does not accept", err)


if __name__ == "__main__":
    unittest.main()
