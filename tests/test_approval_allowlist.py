"""Approval pattern allowlist: approved patterns skip later prompts."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from runner import approval_allowlist as al
from runner.approval import gate_module

_KEYS = (
    "AGENTS_APPROVAL_CMD",
    "AGENTS_APPROVAL_MODE",
    "AGENTS_APPROVAL_ALLOWLIST",
    "AGENTS_APPROVAL_RULES",
    "AGENTS_APPROVAL_POLICY",
)


def _clean() -> dict:
    saved = {k: os.environ.get(k) for k in _KEYS}
    for k in _KEYS:
        os.environ.pop(k, None)
    return saved


def _restore(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class TestAllowlist(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = _clean()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name) / "allowlist.json"
        os.environ["AGENTS_APPROVAL_ALLOWLIST"] = str(self.store)
        self.marker = Path(self._tmp.name) / "approver-hits.txt"
        # Approver: logs a hit, prints {"remember": true}, exits 0 (approve).
        script = Path(self._tmp.name) / "approver.py"
        script.write_text(
            "import os,pathlib,sys\n"
            "sys.stdin.read()\n"
            "pathlib.Path(os.environ['APPROVER_MARKER']).open('a').write('hit\\n')\n"
            "print('{\"remember\": true}')\n"
        )
        os.environ["APPROVER_MARKER"] = str(self.marker)
        os.environ["AGENTS_APPROVAL_CMD"] = f'"{sys.executable}" "{script}"'
        os.environ["AGENTS_APPROVAL_MODE"] = "ask"

    def tearDown(self) -> None:
        _restore(self._saved)

    def _hits(self) -> int:
        try:
            return len(self.marker.read_text().strip().splitlines())
        except OSError:
            return 0

    def test_remember_persists_and_skips(self) -> None:
        mod = {"name": "mcp.terminal", "mutates": True}
        args = {"argv": ["git", "status"]}
        allowed, _ = gate_module(mod, args)
        self.assertTrue(allowed)
        self.assertEqual(self._hits(), 1)
        # Second identical call: allowlist match, no prompt.
        allowed, _ = gate_module(mod, args)
        self.assertTrue(allowed)
        self.assertEqual(self._hits(), 1)
        pats = al.load_patterns()
        self.assertEqual(len(pats), 1)

    def test_explicit_pattern_matches_summary(self) -> None:
        al.remember("mcp.terminal", {}, pattern="mcp.terminal *git status*")
        self.assertTrue(al.is_allowed("mcp.terminal", {"argv": ["git", "status"]}))
        self.assertFalse(al.is_allowed("mcp.terminal", {"argv": ["git", "push"]}))

    def test_off_env_disables(self) -> None:
        os.environ["AGENTS_APPROVAL_ALLOWLIST"] = "off"
        self.assertIsNone(al.store_path())
        self.assertEqual(al.load_patterns(), [])

    def test_default_path_under_host(self) -> None:
        from unittest.mock import patch

        os.environ.pop("AGENTS_APPROVAL_ALLOWLIST", None)
        try:
            from agents_workspace import Host

            host = Host.from_env()
        except Exception:
            self.skipTest("no host")
        path = al.store_path()
        self.assertIsNotNone(path)
        # Active workspace context scopes the store under <host>/approvals/;
        # without any workspace the legacy host-wide file is used.
        from agents_workspace import Workspace  # noqa: F401

        found = host.list_workspaces()
        if found:
            self.assertEqual(path.parent, host.dir / "approvals")
            self.assertTrue(path.name.startswith("ws-"))
            self.assertTrue(path.name.endswith(".allowlist.json"))
        else:
            self.assertEqual(path, host.approval_allowlist)

        with tempfile.TemporaryDirectory() as tmp:
            isolated = Host(dir=Path(tmp) / "host")
            ws = isolated.create("demo")
            with patch.dict(os.environ, {"AGENTS_HOST_DIR": str(isolated.dir)}, clear=False):
                os.environ.pop("AGENTS_APPROVAL_ALLOWLIST", None)
                ws_path = isolated.dir / "approvals" / "ws-demo.allowlist.json"
                self.assertEqual(al.store_path(), ws_path)
                self.assertEqual(al.store_path(ws), ws_path)

    def test_workspace_inside_override_paths_rejected(self) -> None:
        from unittest.mock import patch
        from agents_workspace import Host

        with tempfile.TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            ws = host.create("demo")
            external_workdir = Path(tmp) / "ext-work"
            external_workdir.mkdir()
            ad_hoc_root = Path(tmp) / "ad-hoc-ws"
            ad_hoc_root.mkdir()

            bad_cases = [
                (ws.dir / "approval-allowlist.json", {}),
                (ws.run / "approval-allowlist.json", {}),
                (host.workspaces_dir / "other" / "allowlist.json", {}),
                (ws.workdir / "allowlist.json", {}),
                (external_workdir / "allowlist.json", {"AGENTS_WORKDIR": str(external_workdir)}),
                (ad_hoc_root / "allowlist.json", {"AGENTS_WORKSPACE_ROOT": str(ad_hoc_root)}),
            ]
            for bad_path, extra_env in bad_cases:
                bad_path.parent.mkdir(parents=True, exist_ok=True)
                bad_path.write_text(json.dumps({"patterns": ["*"]}), encoding="utf-8")
                env = {
                    "AGENTS_HOST_DIR": str(host.dir),
                    "AGENTS_APPROVAL_ALLOWLIST": str(bad_path),
                    **extra_env,
                }
                with patch.dict(os.environ, env, clear=False):
                    self.assertIsNone(al.store_path(), msg=str(bad_path))
                    self.assertIsNone(al.store_path(ws), msg=str(bad_path))
                    self.assertEqual(al.load_patterns(ws), [], msg=str(bad_path))
                    self.assertFalse(
                        al.is_allowed("mcp.terminal", {"argv": ["git", "status"]}, workspace=ws),
                        msg=str(bad_path),
                    )
                    self.assertIsNone(
                        al.remember("mcp.terminal", {"argv": ["git", "push"]}, workspace=ws),
                        msg=str(bad_path),
                    )
                # Ensure gate_module still prompts and does not trust poisoned ws allowlist.
                with patch.dict(os.environ, env, clear=False):
                    allowed, _ = gate_module(
                        {"name": "mcp.terminal", "mutates": True},
                        {"argv": ["git", "status"]},
                        workspace=ws,
                    )
                    self.assertTrue(allowed)
                    self.assertEqual(self._hits(), 1)
                    # Reset marker hits for next iteration
                    self.marker.write_text("", encoding="utf-8")

            # A path under host (outside workspaces/) remains accepted.
            host_custom = host.dir / "custom-allowlist.json"
            with patch.dict(
                os.environ,
                {"AGENTS_HOST_DIR": str(host.dir), "AGENTS_APPROVAL_ALLOWLIST": str(host_custom)},
                clear=False,
            ):
                self.assertEqual(al.store_path(ws), host_custom)

    def test_import_error_fallback(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            host_dir = Path(tmp) / "host"
            env = {k: v for k, v in os.environ.items() if k != "AGENTS_APPROVAL_ALLOWLIST"}
            env["AGENTS_HOST_DIR"] = str(host_dir)
            with patch.dict(os.environ, env, clear=True):
                with patch.dict(sys.modules, {"agents_workspace": None}):
                    self.assertEqual(al.store_path(), host_dir / "approval-allowlist.json")
                    os.environ["AGENTS_APPROVAL_ALLOWLIST"] = str(
                        host_dir / "workspaces" / "demo" / "allowlist.json"
                    )
                    self.assertIsNone(al.store_path())

    def test_deny_rule_still_wins(self) -> None:
        al.remember("mcp.terminal", {"argv": ["git", "status"]})
        os.environ["AGENTS_APPROVAL_RULES"] = json.dumps(
            [{"action": "tool:mcp.terminal", "decision": "deny"}]
        )
        mod = {"name": "mcp.terminal", "mutates": True}
        allowed, denial = gate_module(mod, {"argv": ["git", "status"]})
        self.assertFalse(allowed)
        self.assertIn("denied", denial.lower())


if __name__ == "__main__":
    unittest.main()


class TestWorkspaceScopedAllowlist(unittest.TestCase):
    """agents-harness#37: <host>/approvals/ws-<id>.allowlist.json."""

    def test_patterns_isolated_per_workspace(self):
        import os
        from unittest.mock import patch
        from agents_workspace import Host
        from runner.approval_allowlist import is_allowed, remember, store_path

        with tempfile.TemporaryDirectory() as tmp:
            host = Host.open(tmp)
            ws_a = host.create("ws-a")
            host.create("ws-b")
            env = {"AGENTS_HOST_DIR": tmp, "AGENTS_WORKSPACE": "ws-a"}
            with patch.dict(os.environ, env, clear=False):
                path = store_path()
                self.assertEqual(
                    path, host.dir / "approvals" / "ws-ws-a.allowlist.json")
                remember("mcp.terminal", {"cmd": "git status"})
                self.assertTrue(is_allowed("mcp.terminal", {"cmd": "git status"}))
            env["AGENTS_WORKSPACE"] = "ws-b"
            with patch.dict(os.environ, env, clear=False):
                self.assertFalse(is_allowed("mcp.terminal", {"cmd": "git status"}))
                self.assertEqual(
                    store_path(),
                    host.dir / "approvals" / "ws-ws-b.allowlist.json")

    def test_explicit_workspace_arg(self):
        import os
        from unittest.mock import patch
        from agents_workspace import Host
        from runner.approval_allowlist import store_path

        with tempfile.TemporaryDirectory() as tmp:
            host = Host.open(tmp)
            ws_a = host.create("ws-a")
            with patch.dict(os.environ, {"AGENTS_HOST_DIR": tmp}, clear=False):
                self.assertEqual(
                    store_path(ws_a),
                    host.dir / "approvals" / "ws-ws-a.allowlist.json")
