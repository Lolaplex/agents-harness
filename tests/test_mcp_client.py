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
    mcp_cache_path,
    mcp_config_path,
    modules_from_descriptors,
    remote_modules,
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

    def test_paths_use_host_without_creating_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            host_dir = Path(tmp) / "host"
            host_dir.mkdir()
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("AGENTS_MCP_CONFIG_FILE", "AGENTS_MCP_CONFIG", "AGENTS_MCP_CACHE_FILE", "AGENTS_MCP_CACHE", "AGENTS_WORKSPACE")
            }
            env["AGENTS_HOST_DIR"] = str(host_dir)
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(mcp_config_path(), host_dir.resolve() / "mcp.json")
                self.assertEqual(mcp_cache_path(), host_dir.resolve() / "cache" / "mcp-tools-cache.json")
                self.assertIsNone(load_mcp_config())
                self.assertFalse((host_dir / "workspaces").exists())

    def test_host_ws_overlay_interpolates_and_ws_mcp_never_interpolates(self):
        from agents_workspace import Host

        with tempfile.TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            ws = host.create("alpha")
            host.mcp_json.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "shared": {"url": "https://host.test", "headers": {"Authorization": "Bearer ${HOST_TOK}"}},
                            "override": {"url": "https://base.test"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (host.dir / "mcp").mkdir()
            (host.dir / "mcp" / "ws-alpha.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "override": {"url": "https://ws-host.test", "headers": {"X-Key": "${WS_HOST_TOK}"}},
                            "hostonly": {"url": "https://hostonly.test", "headers": {"X-Key": "${WS_HOST_TOK}"}},
                        }
                    }
                ),
                encoding="utf-8",
            )
            ws.mcp_json.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "untrusted": {
                                "url": "https://agent.test/${HOST_TOK}",
                                "headers": {"Authorization": "Bearer ${RELAY_SECRET}"},
                                "env": {"LEAK": "${WS_HOST_TOK}"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            env = {
                "AGENTS_HOST_DIR": str(host.dir),
                "AGENTS_WORKSPACE": "alpha",
                "HOST_TOK": "h_secret",
                "WS_HOST_TOK": "wh_secret",
                "RELAY_SECRET": "r_secret",
            }
            with patch.dict(os.environ, env, clear=True):
                cfg = load_mcp_config()
            self.assertIsNotNone(cfg)
            assert cfg is not None
            servers = cfg["mcpServers"]
            self.assertEqual(servers["shared"]["headers"]["Authorization"], "Bearer h_secret")
            self.assertEqual(servers["override"]["url"], "https://ws-host.test")
            self.assertEqual(servers["override"]["headers"]["X-Key"], "wh_secret")
            self.assertEqual(servers["hostonly"]["headers"]["X-Key"], "wh_secret")
            # Agent-writable <ws>/mcp.json must NOT interpolate ${ENV}.
            self.assertEqual(servers["untrusted"]["url"], "https://agent.test/${HOST_TOK}")
            self.assertEqual(servers["untrusted"]["headers"]["Authorization"], "Bearer ${RELAY_SECRET}")
            self.assertEqual(servers["untrusted"]["env"]["LEAK"], "${WS_HOST_TOK}")

    def test_remote_modules_loads_host_ws_overlay_without_root_mcp(self):
        from agents_workspace import Host

        with tempfile.TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            host.create("alpha")
            (host.dir / "mcp").mkdir()
            (host.dir / "mcp" / "ws-alpha.json").write_text(
                json.dumps({"mcpServers": {"srv": {"url": "https://example.test", "headers": {"X": "${TOK}"}}}}),
                encoding="utf-8",
            )
            seen_cfg = {}

            def fake_discover(cfg):
                seen_cfg.update(cfg)
                return [{"name": "mcp.srv.ping", "kind": "mcp_remote"}]

            env = {
                "AGENTS_HOST_DIR": str(host.dir),
                "AGENTS_WORKSPACE": "alpha",
                "AGENTS_MCP_CACHE_SEC": "0",
                "TOK": "sek",
            }
            with patch.dict(os.environ, env, clear=True):
                with patch("runner.mcp_client.discover_modules", side_effect=fake_discover):
                    rows = remote_modules()
            self.assertEqual(rows, [{"name": "mcp.srv.ping", "kind": "mcp_remote"}])
            self.assertEqual(seen_cfg["mcpServers"]["srv"]["headers"]["X"], "sek")


if __name__ == "__main__":
    unittest.main()
