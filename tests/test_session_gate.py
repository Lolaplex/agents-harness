"""runner.session_gate — first host-local tool binds the session to this host.

harness#42: the gate runs after approval and before execution, on the
canonical module name (never the raw LLM string), and fails closed.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner import session_gate
from runner.cordis_tools import run_cordis_tool


def _call(name: str, arguments: dict | None = None) -> dict:
    return {"function": {"arguments": {"name": name, "arguments": arguments or {}}}}


def _success_record() -> dict:
    return {"status": "SUCCESS", "exit_code": 0, "stdout": "ok", "stderr": ""}


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
                     "mcp.browser", "mcp.browser.open", "mcp.delegate",
                     "mcp.delegate.spawn", "mcp.voice", "mcp.voice.say",
                     "mcp.keys", "mcp.attest", "mcp.fs", "mcp.proc",
                     "mcp.process", "repo.pre_pr", "vision.analyze",
                     "fs.read", "proc.list", "git.diff", "process",
                     "terminal.run", "workdir"):
            self.assertTrue(session_gate.is_host_local(name), name)
        for name in ("mcp.memory", "mcp.memory.add", "mcp.calendar",
                     "mcp.traces", "persona.ask", "persona.inbox",
                     "mcp.schedule.add", "skill.load", "mcp.model",
                     "read_content", "list_catalog", "call_job", ""):
            self.assertFalse(session_gate.is_host_local(name), name)

    def test_classification_is_case_insensitive(self):
        for name in ("GIT", "MCP.TERMINAL", "Mcp.Browser.Open", "MCP.VOICE"):
            self.assertTrue(session_gate.is_host_local(name), name)

    def test_manifest_and_command_spec_host_local_flag(self):
        self.assertTrue(
            session_gate.is_host_local("mcp.custom", {"name": "mcp.custom", "host_local": True})
        )
        pinned = {
            "name": "mcp.custom.exec",
            "command_pin": "exec",
            "commands": {"exec": {"host_local": True}},
        }
        self.assertTrue(session_gate.is_host_local("mcp.custom.exec", pinned))
        plain = {
            "name": "mcp.custom",
            "commands": {"read": {"side_effects": "read-only"}},
        }
        self.assertFalse(session_gate.is_host_local("mcp.custom", plain))

    def test_stdio_mcp_remote_is_host_local_http_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "mcp.json"
            cfg.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "localbin": {"command": "python", "args": ["srv.py"]},
                            "webby": {"url": "https://example.test/mcp"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"AGENTS_MCP_CONFIG_FILE": str(cfg)}):
                stdio_mod = {"name": "mcp.localbin.x", "kind": "mcp_remote", "mcp_server": "localbin"}
                http_mod = {"name": "mcp.webby.y", "kind": "mcp_remote", "mcp_server": "webby"}
                self.assertTrue(session_gate.is_host_local("mcp.localbin.x", stdio_mod))
                self.assertFalse(session_gate.is_host_local("mcp.webby.y", http_mod))

    def test_no_env_no_hook_is_noop(self):
        os.environ.pop("AGENTS_SESSION_BIND_CMD", None)
        with patch("subprocess.run") as run:
            self.assertTrue(session_gate.maybe_bind("ses_1", "git"))
        run.assert_not_called()

    def test_fires_once_on_first_host_local_tool(self):
        os.environ["AGENTS_SESSION_BIND_CMD"] = "gate {session} {tool}"
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b""
            run.return_value.stderr = b""
            self.assertFalse(session_gate.maybe_bind("ses_1", "mcp.memory"))
            self.assertTrue(session_gate.maybe_bind("ses_1", "git"))
            self.assertFalse(session_gate.maybe_bind("ses_1", "mcp.terminal"))
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
            run.return_value.returncode = 0
            run.return_value.stdout = b""
            run.return_value.stderr = b""
            session_gate.maybe_bind("ses_9", "git", WS())
        self.assertIn("wsA", run.call_args[0][0])

    def test_failed_bind_raises_and_never_latches(self):
        os.environ["AGENTS_SESSION_BIND_CMD"] = "gate {session}"
        with patch("subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = b""
            run.return_value.stderr = b"boom"
            with self.assertRaises(session_gate.SessionGateError):
                session_gate.maybe_bind("ses_1", "git")
            self.assertFalse(session_gate._fired)
            with self.assertRaises(session_gate.SessionGateError):
                session_gate.maybe_bind("ses_1", "git")
        self.assertEqual(run.call_count, 2)

    def test_bind_oserror_raises_and_never_latches(self):
        os.environ["AGENTS_SESSION_BIND_CMD"] = "gate {session}"
        with patch("subprocess.run", side_effect=OSError("no binary")):
            with self.assertRaises(session_gate.SessionGateError):
                session_gate.maybe_bind("ses_1", "git")
        self.assertFalse(session_gate._fired)

    def test_missing_session_when_configured_raises(self):
        os.environ["AGENTS_SESSION_BIND_CMD"] = "gate {session}"
        with self.assertRaises(session_gate.SessionGateError):
            session_gate.maybe_bind("", "git")

    def test_bad_template_fails_closed(self):
        os.environ["AGENTS_SESSION_BIND_CMD"] = "x {bogus}"
        with patch("subprocess.run") as run:
            with self.assertRaises(session_gate.SessionGateError):
                session_gate.maybe_bind("ses_1", "git")
        run.assert_not_called()
        self.assertFalse(session_gate._fired)

    def test_gate_hook_exception_fails_closed(self):
        def boom(_s, _t, _w):
            raise RuntimeError("hook down")

        session_gate.register_session_gate(boom)
        with self.assertRaises(session_gate.SessionGateError):
            session_gate.maybe_bind("ses_1", "git")
        self.assertFalse(session_gate._fired)

    def test_bind_argv_splits_before_format_no_injection(self):
        argv = session_gate._bind_argv(
            '"C:\\Python312\\python.exe" -m klanker.bind --session {session} --tool {tool}',
            "ses_1",
            "a --inject b",
            None,
        )
        if os.name == "nt":
            self.assertEqual(argv[0], "C:\\Python312\\python.exe")
        self.assertIn("--session", argv)
        self.assertIn("ses_1", argv)
        # A whitespace value stays ONE argv element — never extra args.
        self.assertEqual(argv[-1], "a --inject b")
        self.assertNotIn("--inject", argv)

    def test_bind_result_workdir_switches_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_gate.register_session_gate(
                lambda _s, _t, _w: {"bound": True, "workdir": tmp}
            )
            self.assertTrue(session_gate.maybe_bind("ses_1", "git"))
            self.assertEqual(os.environ["AGENTS_WORKDIR"], str(Path(tmp)))

    def test_bind_result_invalid_workdir_fails(self):
        session_gate.register_session_gate(
            lambda _s, _t, _w: {"bound": True, "workdir": "no/such/dir-xyz"}
        )
        with self.assertRaises(session_gate.SessionGateError):
            session_gate.maybe_bind("ses_1", "git")
        self.assertFalse(session_gate._fired)

    def test_bind_cmd_json_workdir(self):
        os.environ["AGENTS_SESSION_BIND_CMD"] = "gate {session}"
        with tempfile.TemporaryDirectory() as tmp:
            with patch("subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = json.dumps(
                    {"bound": True, "workdir": tmp}
                ).encode()
                run.return_value.stderr = b""
                self.assertTrue(session_gate.maybe_bind("ses_1", "git"))
            self.assertEqual(os.environ["AGENTS_WORKDIR"], str(Path(tmp)))

    def test_gate_hook_false_fails_closed(self):
        # klanker session_bind_gate returns bool: False is a decline and
        # must raise — collapsing it to {} would run the tool unbound.
        session_gate.register_session_gate(lambda _s, _t, _w: False)
        with self.assertRaises(session_gate.SessionGateError):
            session_gate.maybe_bind("ses_1", "git")
        self.assertFalse(session_gate._fired)

    def test_gate_hook_true_binds(self):
        session_gate.register_session_gate(lambda _s, _t, _w: True)
        self.assertTrue(session_gate.maybe_bind("ses_1", "git"))

    def test_gate_hook_already_bound_dict_binds(self):
        session_gate.register_session_gate(
            lambda _s, _t, _w: {"bound": True, "already_bound": True}
        )
        self.assertTrue(session_gate.maybe_bind("ses_1", "git"))

    def test_bind_cmd_bound_false_fails_closed(self):
        os.environ["AGENTS_SESSION_BIND_CMD"] = "gate {session}"
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps(
                {"bound": False, "workdir": "", "reason": "already bound"}
            ).encode()
            run.return_value.stderr = b""
            with self.assertRaises(session_gate.SessionGateError):
                session_gate.maybe_bind("ses_1", "git")
        self.assertFalse(session_gate._fired)

    def test_bind_cmd_bare_false_fails_closed(self):
        os.environ["AGENTS_SESSION_BIND_CMD"] = "gate {session}"
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b"false"
            run.return_value.stderr = b""
            with self.assertRaises(session_gate.SessionGateError):
                session_gate.maybe_bind("ses_1", "git")
        self.assertFalse(session_gate._fired)


class TestPreExecutionGate(unittest.TestCase):
    """maybe_bind runs inside run_cordis_tool: after approval, before exec."""

    def setUp(self):
        session_gate._reset_for_tests()
        self._env = dict(os.environ)
        os.environ.pop("AGENTS_SESSION_BIND_CMD", None)
        self._overlay = tempfile.TemporaryDirectory()
        overlay = Path(self._overlay.name)
        for mod_name in ("mcp.browser.open", "mcp.delegate.spawn", "mcp.voice"):
            (overlay / f"{mod_name}.json").write_text(
                json.dumps(
                    {
                        "name": mod_name,
                        "kind": "mcp",
                        "when": "on_request",
                        "cadence": "on_request",
                        "verb": "python -c pass",
                        "rests_on": "test overlay",
                        "mutates": True,
                    }
                ),
                encoding="utf-8",
            )
        host = overlay / "host"
        host.mkdir()
        self._env_patch = patch.dict(
            os.environ,
            {
                "AGENTS_MODULES_DIR": str(overlay),
                "AGENTS_HOST_DIR": str(host),
                "AGENTS_CORDIS_EXPOSE": "0",
            },
        )
        self._env_patch.start()
        # Remote discovery is config-driven; tests resolve manifests only.
        self._remote_patch = patch(
            "runner.mcp_client.remote_modules", return_value=[]
        )
        self._remote_patch.start()

    def tearDown(self):
        self._remote_patch.stop()
        self._env_patch.stop()
        self._overlay.cleanup()
        os.environ.clear()
        os.environ.update(self._env)
        session_gate._reset_for_tests()

    def _run_gated(self, name: str, arguments: dict | None = None):
        events: list[tuple] = []

        def bind_fn(session, tool, _ws):
            events.append(("bind", session, tool))

        session_gate.register_session_gate(bind_fn)
        with patch(
            "runner.cordis_tools.with_discovered_commands", lambda m: m
        ), patch(
            "runner.cordis_tools.execute_job",
            side_effect=lambda mod, **kw: events.append(("exec", mod["name"]))
            or _success_record(),
        ):
            outcome = run_cordis_tool(
                "call_job", _call(name, arguments), session="ses_root"
            )
        return outcome, events

    def test_host_local_tools_bind_before_execution(self):
        for name, canonical in (
            ("mcp_terminal", "mcp.terminal"),
            ("MCP.TERMINAL", "mcp.terminal"),
            ("GIT", "git"),
            ("mcp.browser.open", "mcp.browser.open"),
            ("mcp.delegate.spawn", "mcp.delegate.spawn"),
            ("mcp.voice", "mcp.voice"),
        ):
            session_gate._reset_for_tests()
            outcome, events = self._run_gated(
                name, {"command": "status"} if canonical == "git" else {}
            )
            self.assertTrue(outcome.success, f"{name}: {outcome.text}")
            self.assertEqual(
                events,
                [("bind", "ses_root", canonical), ("exec", canonical)],
                name,
            )

    def test_stdio_mcp_remote_binds_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "mcp.json"
            cfg.write_text(
                json.dumps(
                    {"mcpServers": {"localbin": {"command": "python", "args": ["s.py"]}}}
                ),
                encoding="utf-8",
            )
            overlay = Path(self._overlay.name)
            (overlay / "mcp.localbin.run.json").write_text(
                json.dumps(
                    {
                        "name": "mcp.localbin.run",
                        "kind": "mcp_remote",
                        "when": "on_request",
                        "cadence": "on_request",
                        "verb": "mcp://localbin/run",
                        "rests_on": "test",
                        "mutates": True,
                        "mcp_server": "localbin",
                        "mcp_tool": "run",
                    }
                ),
                encoding="utf-8",
            )
            events: list[tuple] = []
            session_gate.register_session_gate(
                lambda s, t, _w: events.append(("bind", s, t))
            )
            with patch.dict(
                os.environ, {"AGENTS_MCP_CONFIG_FILE": str(cfg)}
            ), patch(
                "runner.mcp_client.call_remote",
                side_effect=lambda *a, **kw: events.append(("exec", "remote"))
                or "pong",
            ):
                outcome = run_cordis_tool(
                    "call_job", _call("mcp.localbin.run"), session="ses_root"
                )
            self.assertTrue(outcome.success, outcome.text)
            self.assertEqual(
                events,
                [("bind", "ses_root", "mcp.localbin.run"), ("exec", "remote")],
            )

    def test_read_content_portable_never_binds(self):
        events: list[tuple] = []
        session_gate.register_session_gate(
            lambda s, t, _w: events.append(("bind", s, t))
        )
        outcome = run_cordis_tool(
            "read_content",
            {"function": {"arguments": {"digest": "", "next": True}}},
            session="ses_root",
        )
        self.assertEqual(events, [])
        outcome = run_cordis_tool(
            "call_job", _call("read_content"), session="ses_root"
        )
        self.assertEqual(events, [])
        self.assertFalse(session_gate._fired)

    def test_failed_bind_blocks_tool_execution(self):
        os.environ["AGENTS_SESSION_BIND_CMD"] = "gate {session}"
        with patch(
            "runner.cordis_tools.with_discovered_commands", lambda m: m
        ), patch("subprocess.run") as run, patch(
            "runner.cordis_tools.execute_job"
        ) as ex:
            run.return_value.returncode = 1
            run.return_value.stdout = b""
            run.return_value.stderr = b"no bind"
            outcome = run_cordis_tool(
                "call_job", _call("git", {"command": "status"}), session="ses_root"
            )
        self.assertFalse(outcome.success)
        self.assertIn("session host bind failed", str(outcome.text))
        ex.assert_not_called()
        self.assertFalse(session_gate._fired)

    def test_denied_tool_never_reaches_bind(self):
        events: list[tuple] = []
        session_gate.register_session_gate(
            lambda s, t, _w: events.append(("bind", s, t))
        )
        with patch(
            "runner.cordis_tools.with_discovered_commands", lambda m: m
        ), patch(
            "runner.cordis_tools.gate_module",
            return_value=(False, "Denied: blocked"),
        ), patch("runner.cordis_tools.execute_job") as ex:
            outcome = run_cordis_tool(
                "call_job", _call("git", {"command": "status"}), session="ses_root"
            )
        self.assertFalse(outcome.success)
        self.assertIn("Denied: blocked", str(outcome.text))
        self.assertEqual(events, [])
        ex.assert_not_called()

    def test_bind_workdir_routes_tool_execution(self):
        with tempfile.TemporaryDirectory() as wt:
            seen: dict = {}
            session_gate.register_session_gate(
                lambda _s, _t, _w: {"bound": True, "workdir": wt}
            )
            def fake_exec(mod, **kw):
                seen["workdir"] = os.environ.get("AGENTS_WORKDIR")
                return _success_record()

            with patch(
                "runner.cordis_tools.with_discovered_commands", lambda m: m
            ), patch(
                "runner.cordis_tools.execute_job", side_effect=fake_exec
            ):
                outcome = run_cordis_tool(
                    "call_job", _call("git", {"command": "status"}), session="ses_root"
                )
            self.assertTrue(outcome.success, outcome.text)
            self.assertEqual(seen["workdir"], str(Path(wt)))


class TestChildTurnBinding(unittest.TestCase):
    def setUp(self):
        session_gate._reset_for_tests()
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        session_gate._reset_for_tests()

    def test_tool_env_strips_gate_env_unless_extra_allow(self):
        from agents_workspace import Host

        from runner.executor import _tool_env

        with tempfile.TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            ws = host.create("demo")
            env = {
                "AGENTS_HOST_DIR": str(host.dir),
                "AGENTS_WORKSPACE": "demo",
                "AGENTS_SESSION_BIND_CMD": "bind {session}",
                "AGENTS_PARENT_SESSION": "ses_root",
            }
            manifest = {"name": "x", "verb": "python -c pass"}
            forwarded = dict(manifest)
            forwarded["extra_allow"] = [
                "AGENTS_SESSION_BIND_CMD",
                "AGENTS_PARENT_SESSION",
            ]
            with patch.dict(os.environ, env, clear=True):
                stripped = _tool_env(ws, manifest)
                kept = _tool_env(ws, forwarded)
        self.assertNotIn("AGENTS_SESSION_BIND_CMD", stripped)
        self.assertNotIn("AGENTS_PARENT_SESSION", stripped)
        self.assertEqual(kept["AGENTS_SESSION_BIND_CMD"], "bind {session}")
        self.assertEqual(kept["AGENTS_PARENT_SESSION"], "ses_root")

    def test_delegate_spawn_binds_parent_before_child(self):
        from agents_workspace import Host

        from runner import delegate

        calls: list[tuple] = []
        session_gate.register_session_gate(
            lambda s, t, _w: calls.append((s, t))
        )
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            host.create("demo")
            env = {
                "AGENTS_HOST_DIR": str(host.dir),
                "AGENTS_WORKSPACE": "demo",
                "AGENTS_PARENT_SESSION": "ses_root",
            }
            with patch.dict(os.environ, env, clear=True), patch(
                "runner.delegate.subprocess.Popen"
            ) as pop:
                pop.return_value.pid = 4242
                res = delegate.spawn("hello task")
        self.assertTrue(res["id"].startswith("dlg_"))
        self.assertIn(("ses_root", "mcp.delegate"), calls)

    def test_delegate_spawn_child_gets_post_bind_workdir(self):
        from agents_workspace import Host

        from runner import delegate

        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "worktree"
            wt.mkdir()
            main_repo = Path(tmp) / "main"
            session_gate.register_session_gate(
                lambda _s, _t, _w: {"bound": True, "workdir": str(wt)}
            )
            host = Host(dir=Path(tmp) / "host")
            host.create("demo")
            env = {
                "AGENTS_HOST_DIR": str(host.dir),
                "AGENTS_WORKSPACE": "demo",
                "AGENTS_PARENT_SESSION": "ses_root",
                "AGENTS_WORKDIR": str(main_repo),
            }
            captured: dict = {}

            class FakePopen:
                def __init__(self, argv, **kw):
                    captured.update(kw.get("env") or {})
                    self.pid = 4321

            with patch.dict(os.environ, env, clear=True), patch(
                "runner.delegate.subprocess.Popen", FakePopen
            ):
                res = delegate.spawn("wt check")
        self.assertTrue(res["id"].startswith("dlg_"))
        self.assertEqual(
            str(Path(captured.get("AGENTS_WORKDIR", "")).resolve()),
            str(wt.resolve()),
        )


class TestBypassPaths(unittest.TestCase):
    def setUp(self):
        session_gate._reset_for_tests()
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        session_gate._reset_for_tests()

    def test_schedule_add_verb_rearms_approval(self):
        from runner.approval import needs_approval
        from runner.modules import find_module

        mod = find_module("mcp.schedule.add")
        self.assertIsNotNone(mod)
        with patch.dict(
            os.environ,
            {"AGENTS_APPROVAL_CMD": "approve", "AGENTS_APPROVAL_MODE": "ask"},
        ):
            self.assertFalse(needs_approval(mod, {"text": "hi", "at": "+1h"}))
            self.assertTrue(
                needs_approval(mod, {"verb": "python -m anything", "at": "+1h"})
            )

    def test_schedule_add_argv_verb_rearms_approval(self):
        # Raw argv smuggles --verb past the waiver: the approval_ask:false
        # path must scan argv tokens too (harness#42 follow-up).
        from runner.approval import needs_approval
        from runner.modules import find_module

        mod = find_module("mcp.schedule.add")
        self.assertIsNotNone(mod)
        with patch.dict(
            os.environ,
            {"AGENTS_APPROVAL_CMD": "approve", "AGENTS_APPROVAL_MODE": "ask"},
        ):
            self.assertFalse(
                needs_approval(mod, {"argv": ["--at", "+1s", "--text", "hi"]})
            )
            self.assertTrue(
                needs_approval(
                    mod, {"argv": ["--at", "+1s", "--verb", "python -c x"]}
                )
            )
            self.assertTrue(
                needs_approval(mod, {"argv": ["--at=+1s", "--verb=python -c x"]})
            )
            self.assertTrue(
                needs_approval(mod, {"arguments": {"argv": ["--verb", "x"]}})
            )

    def test_schedule_add_argv_verb_abbreviations_rearm_approval(self):
        from runner.approval import needs_approval
        from runner.modules import find_module

        mod = find_module("mcp.schedule.add")
        self.assertIsNotNone(mod)
        cases = (
            {"argv": ["--at", "+1s", "--v", "python -c x"]},
            {"argv": ["--at", "+1s", "--ve", "python -c x"]},
            {"argv": ["--at=+1s", "--ver=python -c x"]},
            {"argv": "--at +1s --v 'python -c x'"},
            {"argv": "--at=+1s --ver='python -c x'"},
        )
        with patch.dict(
            os.environ,
            {"AGENTS_APPROVAL_CMD": "approve", "AGENTS_APPROVAL_MODE": "ask"},
        ):
            for args in cases:
                with self.subTest(args=args):
                    self.assertTrue(needs_approval(mod, args))

    def test_schedule_add_non_dict_arguments_verb_rearms_approval(self):
        from runner.approval import needs_approval
        from runner.modules import find_module

        mod = find_module("mcp.schedule.add")
        self.assertIsNotNone(mod)
        cases = (
            {"arguments": ["--v", "python -c x"]},
            {"arguments": ["--ver=python -c x"]},
            {"arguments": "--v 'python -c x'"},
            {"arguments": "--ver='python -c x'"},
            ["--v", "python -c x"],
            "--ver='python -c x'",
        )
        with patch.dict(
            os.environ,
            {"AGENTS_APPROVAL_CMD": "approve", "AGENTS_APPROVAL_MODE": "ask"},
        ):
            for args in cases:
                with self.subTest(args=args):
                    self.assertTrue(needs_approval(mod, args))

    def test_ws_config_allow_rule_never_permits_stdio(self):
        # <ws>/config.json is agent-writable: an allow rule written there
        # must never authorize a ws-overlay stdio binary. Only the host
        # agents.json + env policy layers count.
        from agents_workspace import Host

        from runner.mcp_client import _ws_stdio_permitted, load_mcp_config

        with tempfile.TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            ws = host.create("alpha")
            ws.config.write_text(
                json.dumps(
                    {
                        "approval": {
                            "rules": [
                                {"action": "mcp.stdio:evil", "decision": "allow"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            ws.mcp_json.write_text(
                json.dumps(
                    {"mcpServers": {"evil": {"command": "python", "args": ["x.py"]}}}
                ),
                encoding="utf-8",
            )
            env = {"AGENTS_HOST_DIR": str(host.dir), "AGENTS_WORKSPACE": "alpha"}
            with patch.dict(os.environ, env, clear=True):
                self.assertFalse(_ws_stdio_permitted("evil"))
                cfg = load_mcp_config()
                self.assertNotIn("evil", cfg["mcpServers"])
                # The same rule in host agents.json IS trusted.
                host.agents_json.write_text(
                    json.dumps(
                        {
                            "approval": {
                                "rules": [
                                    {
                                        "action": "mcp.stdio:evil",
                                        "decision": "allow",
                                    }
                                ]
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertTrue(_ws_stdio_permitted("evil"))

    def test_ws_overlay_stdio_needs_policy_allow(self):
        from agents_workspace import Host

        from runner.mcp_client import load_mcp_config

        with tempfile.TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            ws = host.create("alpha")
            ws.mcp_json.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "evil": {"command": "python", "args": ["x.py"]},
                            "ok": {"url": "https://ok.test/mcp"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            env = {"AGENTS_HOST_DIR": str(host.dir), "AGENTS_WORKSPACE": "alpha"}
            with patch.dict(os.environ, env, clear=True):
                cfg = load_mcp_config()
                self.assertIsNotNone(cfg)
                self.assertIn("ok", cfg["mcpServers"])
                self.assertNotIn("evil", cfg["mcpServers"])
                with patch.dict(
                    os.environ,
                    {
                        "AGENTS_APPROVAL_RULES": json.dumps(
                            [{"action": "mcp.stdio:evil", "decision": "allow"}]
                        )
                    },
                ):
                    cfg = load_mcp_config()
                self.assertIn("evil", cfg["mcpServers"])
                with patch.dict(
                    os.environ,
                    {
                        "AGENTS_APPROVAL_RULES": json.dumps(
                            [{"action": "mcp.stdio:*", "decision": "deny"}]
                        )
                    },
                ):
                    cfg = load_mcp_config()
                self.assertNotIn("evil", cfg["mcpServers"])

    def test_mcp_server_call_job_runs_gate(self):
        try:
            import runner.mcp_server as mcp_mod
        except ImportError:
            self.skipTest("mcp extra not installed")
        calls: list[tuple] = []
        session_gate.register_session_gate(
            lambda s, t, _w: calls.append((s, t))
        )
        with patch(
            "runner.mcp_server.execute_job",
            return_value={"status": "SUCCESS", "stdout": "ok", "stderr": ""},
        ) as ex:
            out = json.loads(mcp_mod.call_job("git", session="ses_root"))
        self.assertNotIn("error", out)
        self.assertEqual(calls, [("ses_root", "git")])
        ex.assert_called_once()

    def test_mcp_server_call_job_denied_never_binds(self):
        try:
            import runner.mcp_server as mcp_mod
        except ImportError:
            self.skipTest("mcp extra not installed")
        calls: list[tuple] = []
        session_gate.register_session_gate(
            lambda s, t, _w: calls.append((s, t))
        )
        with patch(
            "runner.mcp_server.gate_module",
            return_value=(False, "Denied: blocked"),
        ), patch("runner.mcp_server.execute_job") as ex:
            out = json.loads(mcp_mod.call_job("git", session="ses_root"))
        self.assertIn("Denied: blocked", out["error"])
        self.assertEqual(calls, [])
        ex.assert_not_called()

    def test_mcp_server_call_job_schedule_runs_gates(self):
        try:
            import runner.mcp_server as mcp_mod
        except ImportError:
            self.skipTest("mcp extra not installed")
        sched = {
            "name": "care.job",
            "kind": "schedule",
            "cadence": "1h",
            "verb": "python -c pass",
        }
        with patch(
            "runner.mcp_server.find_module", return_value=None
        ), patch(
            "runner.mcp_server.list_schedules", return_value=[sched]
        ), patch(
            "runner.mcp_server.gate_module", return_value=(True, "")
        ) as gate, patch.object(
            mcp_mod.session_gate, "maybe_bind", return_value=True
        ) as bind, patch(
            "runner.mcp_server.execute_job",
            return_value={"status": "SUCCESS", "stdout": "ok", "stderr": ""},
        ) as ex:
            out = json.loads(mcp_mod.call_job("care.job", session="ses_root"))
        self.assertNotIn("error", out)
        gate.assert_called_once()
        bind.assert_called_once_with("ses_root", "care.job", mod=sched)
        ex.assert_called_once()

    def test_mcp_server_call_job_schedule_denied_never_binds(self):
        try:
            import runner.mcp_server as mcp_mod
        except ImportError:
            self.skipTest("mcp extra not installed")
        sched = {"name": "care.job", "kind": "schedule", "verb": "python -c pass"}
        with patch(
            "runner.mcp_server.find_module", return_value=None
        ), patch(
            "runner.mcp_server.list_schedules", return_value=[sched]
        ), patch(
            "runner.mcp_server.gate_module",
            return_value=(False, "Denied: blocked"),
        ), patch.object(mcp_mod.session_gate, "maybe_bind") as bind, patch(
            "runner.mcp_server.execute_job"
        ) as ex:
            out = json.loads(mcp_mod.call_job("care.job", session="ses_root"))
        self.assertIn("Denied: blocked", out["error"])
        bind.assert_not_called()
        ex.assert_not_called()


if __name__ == "__main__":
    unittest.main()
