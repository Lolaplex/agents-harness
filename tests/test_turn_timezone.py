"""Turn clock zone without an identity timezone field.

The echo provider below is the local completion stub. It is not a stand-in
for ``UserRecord.timezone``. These tests import the installed identity record
and fail if that field is patched back on.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from runner.user_profile import turn_timezone

_TRACES = importlib.util.find_spec("agents_traces") is not None
_KLANKER = importlib.util.find_spec("klanker") is not None
_RELAY = importlib.util.find_spec("agents_relay") is not None


def _isolated_env(root: Path, *, profile_zone: str = "") -> dict[str, str]:
    agents = root / "agents"
    (agents / "memory").mkdir(parents=True)
    (agents / "workspace").mkdir()
    (agents / "skills").mkdir()
    (root / "traces").mkdir()
    (root / "content").mkdir()
    (root / "modules").mkdir()
    (root / "home").mkdir()
    who = "## Who\n- Name: QA\n"
    if profile_zone:
        who += f"- Timezone: {profile_zone}\n"
    (agents / "memory" / "USER.md").write_text(who, encoding="utf-8")
    (agents / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    env = os.environ.copy()
    env.pop("QA_SHIM_TIMEZONE", None)
    env.pop("QA_PROVIDER_MODE", None)
    env.pop("PYTHONPATH", None)
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("AGENTS_APPROVAL_CMD", None)
    env.pop("AGENTS_APPROVAL_MODE", None)
    env.update(
        {
            "HOME": str(root / "home"),
            "AGENTS_HOME": str(agents),
            "AGENTS_MEMORY_PATH": str(agents / "memory"),
            "AGENTS_WORKSPACE_DIR": str(agents / "workspace"),
            "AGENTS_MODULES_DIR": str(root / "modules"),
            "AGENTS_TRACES_DIR": str(root / "traces"),
            "AGENTS_CONTENT_DIR": str(root / "content"),
            "AGENTS_IDENTITY_PATH": str(agents / "identity.json"),
            "AGENTS_SKILLS_DIR": str(agents / "skills"),
            "AGENTS_MCP_CONFIG": str(agents / "mcp.json"),
            "AGENTS_MEMORY_RULES": "0",
            "AGENTS_NO_UPDATE_CHECK": "1",
            "CI": "1",
            "KLANKER_TICK": "0",
            "AGENTS_TERMINAL_DRIVER": "subprocess",
        }
    )
    return env


class TestTurnTimezone(unittest.TestCase):
    def test_missing_attribute_uses_profile_and_does_not_invent_an_offset(self) -> None:
        bare = SimpleNamespace(id="u_bare", aliases=[])
        self.assertEqual(turn_timezone(bare, {}), "")
        self.assertEqual(turn_timezone(bare, {"timezone": "  "}), "")
        self.assertEqual(turn_timezone(bare, {"timezone": "Europe/Berlin"}), "Europe/Berlin")
        host = SimpleNamespace(timezone="America/New_York")
        self.assertEqual(turn_timezone(host, {"timezone": "Europe/Berlin"}), "America/New_York")
        blank = SimpleNamespace(timezone="  ")
        self.assertEqual(turn_timezone(blank, {"timezone": "Europe/Berlin"}), "Europe/Berlin")

    @unittest.skipUnless(_TRACES, "agents-traces dev package is not installed")
    def test_real_user_record_has_no_timezone_field(self) -> None:
        from agents_traces.identity import UserRecord

        self.assertNotIn("timezone", UserRecord.__dataclass_fields__)
        person = UserRecord(id="u_real", aliases=["http:qa"])
        self.assertEqual(turn_timezone(person, {"timezone": "Europe/Berlin"}), "Europe/Berlin")
        self.assertFalse(hasattr(person, "timezone"))

    @unittest.skipUnless(_TRACES, "agents-traces dev package is not installed")
    def test_installed_clock_keeps_dst_for_the_passed_iana_name(self) -> None:
        from agents_traces.prompt import clock_message

        zone = turn_timezone(SimpleNamespace(id="u"), {"timezone": "Europe/Berlin"})
        summer = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        summer_text = clock_message(summer, timezone_name=zone)["content"]
        winter_text = clock_message(winter, timezone_name=zone)["content"]
        self.assertIn('timezone="Europe/Berlin"', summer_text)
        self.assertIn("+02:00", summer_text)
        self.assertIn("+01:00", winter_text)
        york = turn_timezone(SimpleNamespace(id="u"), {"timezone": "America/New_York"})
        york_text = clock_message(winter, timezone_name=york)["content"]
        self.assertIn("-05:00", york_text)
        self.assertNotIn("+01:00", york_text)
        bad = clock_message(winter, timezone_name="Not/AZone")["content"]
        self.assertIn('timezone="UTC"', bad)

    @unittest.skipUnless(_TRACES and _KLANKER, "klanker and agents-traces dev packages are required")
    def test_cli_echo_turn_survives_missing_identity_timezone(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = _isolated_env(Path(tmp), profile_zone="Europe/Berlin")
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "klanker.turn",
                    "--channel",
                    "http",
                    "--user",
                    "qa",
                    "--session",
                    "qa_tz_cli",
                    "--detached-session",
                    "--message",
                    "ping-timezone",
                    "--complete",
                    "--deliver",
                    "buffered",
                    "--provider",
                    "echo",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
                check=False,
            )
            self.assertNotIn("AttributeError", proc.stderr, proc.stderr)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("ping-timezone", proc.stdout)
            assembled = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "runner.loop",
                    "--channel",
                    "http",
                    "--user",
                    "qa",
                    "--session",
                    "qa_tz_clock",
                    "--detached-session",
                    "--message",
                    "ping-timezone",
                    "--assemble-only",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
                check=False,
            )
            self.assertEqual(assembled.returncode, 0, assembled.stderr)
            clock = next(
                item["content"]
                for item in json.loads(assembled.stdout)["messages"]
                if str(item.get("content") or "").startswith("<clock")
            )
            self.assertIn('timezone="Europe/Berlin"', clock)
            offset = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Berlin")).strftime("%z")
            self.assertIn(f"{offset[:3]}:{offset[3:]}", clock)
            invalid = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "runner.loop",
                    "--channel",
                    "http",
                    "--user",
                    "qa",
                    "--session",
                    "qa_tz_bad",
                    "--detached-session",
                    "--message",
                    "ping-timezone",
                    "--assemble-only",
                ],
                capture_output=True,
                text=True,
                env=_isolated_env(Path(tmp) / "bad", profile_zone="Not/AZone"),
                timeout=60,
                check=False,
            )
            self.assertEqual(invalid.returncode, 0, invalid.stderr)
            self.assertNotIn("AttributeError", invalid.stderr)
            bad_clock = next(
                item["content"]
                for item in json.loads(invalid.stdout)["messages"]
                if str(item.get("content") or "").startswith("<clock")
            )
            self.assertIn('timezone="UTC"', bad_clock)

    @unittest.skipUnless(
        _TRACES and _KLANKER and _RELAY,
        "klanker, relay, and agents-traces dev packages are required",
    )
    def test_relay_http_echo_turn_is_not_502(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = _isolated_env(Path(tmp))
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env["RELAY_HOST"] = "127.0.0.1"
            env["RELAY_PORT"] = str(port)
            env["RELAY_SECRET"] = ""
            proc = subprocess.Popen(
                [sys.executable, "-m", "klanker", "serve", "--no-telegram"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                start_new_session=True,
            )
            url = f"http://127.0.0.1:{port}"
            status = 0
            body = ""
            try:
                deadline = time.time() + 30
                healthy = False
                while time.time() < deadline and proc.poll() is None:
                    try:
                        with urllib.request.urlopen(url + "/health", timeout=2) as resp:
                            if resp.status == 200:
                                healthy = True
                                break
                    except (urllib.error.URLError, TimeoutError, OSError):
                        time.sleep(0.2)
                self.assertTrue(healthy, "serve did not become healthy")
                payload = json.dumps(
                    {
                        "message": "ping-timezone",
                        "user": "qa",
                        "channel": "http",
                        "session": "qa_tz_http",
                        "provider": "echo",
                    }
                ).encode()
                req = urllib.request.Request(
                    url + "/v1/turn",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        status = resp.status
                        body = resp.read().decode()
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    body = exc.read().decode("utf-8", errors="replace")
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                if proc.stdout is not None:
                    proc.stdout.close()
        self.assertEqual(status, 200, body)
        parsed = json.loads(body)
        self.assertEqual(parsed.get("returncode"), 0)
        self.assertIn("ping-timezone", str(parsed.get("reply") or ""))
        self.assertTrue(str(parsed.get("session") or ""))


if __name__ == "__main__":
    unittest.main()
