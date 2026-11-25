"""runner.session_gate — first host-local tool fires the bind cmd once."""
import os
import sys
import unittest
from unittest.mock import patch

from runner import session_gate


class TestSessionGate(unittest.TestCase):
    def setUp(self):
        session_gate._reset_for_tests()
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        session_gate._reset_for_tests()

    def test_host_local_classification(self):
        for name in ("git", "mcp.terminal", "mcp.terminal.run",
                     "mcp.browser", "mcp.delegate", "repo.pre_pr",
                     "vision.analyze", "fs.read", "proc.list", "git.diff"):
            self.assertTrue(session_gate.is_host_local(name), name)
        for name in ("mcp.memory", "mcp.memory.add", "mcp.calendar",
                     "mcp.traces", "persona.ask", "persona.inbox",
                     "mcp.schedule.add", "skill.load", "mcp.model",
                     "list_catalog", "call_job", ""):
            self.assertFalse(session_gate.is_host_local(name), name)

    def test_no_env_is_noop(self):
        os.environ.pop("AGENTS_SESSION_BIND_CMD", None)
        with patch("subprocess.run") as run:
            self.assertTrue(session_gate.maybe_bind("ses_1", "git"))
        run.assert_not_called()

    def test_fires_once_on_first_host_local_tool(self):
        os.environ["AGENTS_SESSION_BIND_CMD"] = (
            f"{sys.executable} -c pass {{session}} {{tool}}")
        with patch("subprocess.run") as run:
            self.assertFalse(session_gate.maybe_bind("ses_1", "mcp.memory"))
            self.assertTrue(session_gate.maybe_bind("ses_1", "git"))
            self.assertFalse(session_gate.maybe_bind("ses_1", "mcp.terminal"))
        # _fired latches before the cmd lookup, so later calls never run
        self.assertEqual(run.call_count, 1)
        argv = run.call_args[0][0]
        self.assertIn("ses_1", argv)
        self.assertIn("git", argv)

    def test_workspace_placeholder(self):
        os.environ["AGENTS_SESSION_BIND_CMD"] = (
            "gate {session} {tool} {workspace}")

        class WS:
            id = "wsA"

        with patch("subprocess.run") as run:
            session_gate.maybe_bind("ses_9", "git", WS())
        self.assertIn("wsA", run.call_args[0][0])

    def test_bad_template_still_latches(self):
        os.environ["AGENTS_SESSION_BIND_CMD"] = "x {bogus}"
        with patch("subprocess.run") as run:
            self.assertTrue(session_gate.maybe_bind("ses_1", "git"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
