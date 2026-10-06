"""External MCP config, allow/deny, and call_job routing. No live server."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.cordis_tools import handle_cordis_tool
from runner.mcp_client import (
    interpolate,
    load_mcp_config,
    modules_from_descriptors,
    reset_cache,
    tool_allowed,
)


class TestMcpClient(unittest.TestCase):
    def tearDown(self):
        reset_cache()

    def test_interpolate_env(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "gh_secret"}):
            self.assertEqual(interpolate("Bearer ${GITHUB_TOKEN}"), "Bearer gh_secret")
            self.assertEqual(interpolate("${MISSING}"), "")

    def test_allow_deny_globs(self):
        cfg = {
            "allow": {"demo": ["search*", "list"]},
            "deny": {"demo": ["search_secret"]},
        }
        self.assertTrue(tool_allowed("demo", "search_docs", cfg))
        self.assertTrue(tool_allowed("demo", "list", cfg))
        self.assertFalse(tool_allowed("demo", "search_secret", cfg))
        self.assertFalse(tool_allowed("demo", "write", cfg))
        open_cfg = {"deny": {"other": ["hide*"]}}
        self.assertTrue(tool_allowed("other", "list", open_cfg))
        self.assertFalse(tool_allowed("other", "hide_me", open_cfg))

    def test_modules_use_schema_and_readonly_hint(self):
        rows = modules_from_descriptors(
            "demo",
            [
                {
                    "name": "search",
                    "description": "find",
                    "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                    "readOnlyHint": True,
                },
                {
                    "name": "write",
                    "description": "store",
                    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
                    "readOnlyHint": False,
                },
            ],
        )
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["mcp.demo.search"]["kind"], "mcp_remote")
        self.assertFalse(by_name["mcp.demo.search"]["mutates"])
        self.assertEqual(by_name["mcp.demo.search"]["parameters"]["properties"]["q"]["type"], "string")
        self.assertTrue(by_name["mcp.demo.write"]["mutates"])
        self.assertEqual(by_name["mcp.demo.write"]["mcp_tool"], "write")

    def test_config_expands_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp.json"
            path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "gh": {"url": "https://example.test/mcp", "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}}
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}):
                cfg = load_mcp_config(path)
        self.assertEqual(cfg["mcpServers"]["gh"]["headers"]["Authorization"], "Bearer tok")

    def test_call_job_routes_remote_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp)
            (overlay / "mcp.demo.echo.json").write_text(
                json.dumps(
                    {
                        "name": "mcp.demo.echo",
                        "kind": "mcp_remote",
                        "when": "on_request",
                        "cadence": "on_request",
                        "verb": "mcp://demo/echo",
                        "rests_on": "test",
                        "expected_exit": 0,
                        "mutates": False,
                        "mcp_server": "demo",
                        "mcp_tool": "echo",
                        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"AGENTS_MODULES_DIR": str(overlay)}):
                with patch("runner.mcp_client.call_remote", return_value="pong") as call:
                    out = handle_cordis_tool(
                        "call_job",
                        {
                            "function": {
                                "arguments": json.dumps(
                                    {"name": "mcp.demo.echo", "arguments": {"q": "hi"}}
                                )
                            }
                        },
                    )
        self.assertIn("pong", out)
        call.assert_called_once_with("demo", "echo", {"q": "hi"})


if __name__ == "__main__":
    unittest.main()
