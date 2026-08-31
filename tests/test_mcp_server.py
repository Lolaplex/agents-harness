import json
import unittest

try:
    import runner.mcp_server as mcp_mod
except ImportError:  # mcp extra not installed
    mcp_mod = None


@unittest.skipIf(mcp_mod is None, "pip install -e '.[mcp]' to run FastMCP tests")
class TestHarnessMcp(unittest.TestCase):
    def test_list_harness_modules(self):
        data = json.loads(mcp_mod.list_harness_modules())
        names = {m["name"] for m in data["modules"]}
        self.assertIn("mcp.traces", names)
        self.assertIn("a2a.peer", names)
        sched = {s["name"] for s in data["schedules"]}
        self.assertIn("traces_ingest", sched)

    def test_assemble_session_requires_user_or_session(self):
        data = json.loads(mcp_mod.assemble_session())
        self.assertIn("error", data)

    def test_call_job_unknown(self):
        data = json.loads(mcp_mod.call_job("no-such-job"))
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
