import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from runner import catalog, helpjson, modules
from runner.catalog import catalog_fingerprint
from runner.delivery import SIDEBAND_PREFIX
from runner.loop import _reasoning_extra, main as loop_main
from runner.modules import list_modules
from runner.pinned import pinned_verbs_prompt_block
from runner.providers import AcpProvider, CompletionRequest, OpenAICompatProvider, CompletionResult, list_providers


class TestW8ProviderCleanup(unittest.TestCase):
    def test_acp_cursor_not_shipped_and_class_still_directly_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "AGENTS_HOST_DIR": str(Path(tmp) / "host"),
                "AGENTS_PROVIDERS_DIR": "",
                "AGENTS_WORKSPACE": "",
            }
            with patch.dict(os.environ, env, clear=False):
                names = {row["name"] for row in list_providers()}
        self.assertNotIn("acp.cursor", names)

        provider = AcpProvider({"name": "acp.cursor", "agent": "cursor"})
        result = provider.complete(CompletionRequest(messages=[{"role": "user", "content": "hi"}]))
        self.assertEqual(provider.kind, "acp")
        self.assertIn("not wired", result.text)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestW8ReasoningOverride(unittest.TestCase):
    def _complete_body(self, extra):
        bodies = []

        def fake_urlopen(req, timeout=None):
            bodies.append(json.loads(req.data.decode("utf-8")))
            return _FakeResponse(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8"))

        provider = OpenAICompatProvider(
            {
                "name": "reasoning.probe",
                "url": "https://api.example/v1",
                "model": "m-test",
                "reasoning": {"enabled": True, "effort": "high"},
            }
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = provider.complete(
                CompletionRequest(
                    messages=[{"role": "user", "content": "hi"}],
                    extra={"stream": False, **extra},
                )
            )
        self.assertEqual(result.text, "ok")
        return bodies[0]

    def test_reasoning_off_overrides_manifest_and_suppresses_thinking(self):
        body = self._complete_body(
            {
                "reasoning": {"enabled": False},
                "thinking": {"type": "enabled", "budget_tokens": 1024},
            }
        )
        self.assertEqual(body["reasoning"], {"enabled": False})
        self.assertNotIn("thinking", body)

    def test_reasoning_auto_keeps_manifest_and_levels_override(self):
        self.assertEqual(_reasoning_extra("auto"), {})
        self.assertEqual(_reasoning_extra("off"), {"reasoning": {"enabled": False}})
        self.assertEqual(
            _reasoning_extra("medium"),
            {"reasoning": {"enabled": True, "effort": "medium"}},
        )
        body = self._complete_body({})
        self.assertEqual(body["reasoning"], {"enabled": True, "effort": "high"})
        body = self._complete_body({"reasoning": {"enabled": True, "effort": "low"}})
        self.assertEqual(body["reasoning"], {"enabled": True, "effort": "low"})

    def test_loop_reasoning_off_reaches_provider_request(self):
        seen = []

        class CaptureProvider:
            def complete(self, req):
                seen.append(req)
                return CompletionResult(text="ok")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "AGENTS_HOST_DIR": str(root / "host"),
                "AGENTS_IDENTITY_PATH": str(root / "identity.json"),
                "AGENTS_TRACES_DIR": str(root / "traces"),
                "AGENTS_NO_UPDATE_CHECK": "1",
                "AGENTS_PROJECT_CONTEXT": "0",
                "AGENTS_MEMORY_RULES": "0",
                "AGENTS_CORDIS_EXPOSE": "0",
                "AGENTS_WORKSPACE": "",
                "HARNESS_REASONING": "",
            }
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, env, clear=False), patch(
                "runner.loop.get_provider", return_value=CaptureProvider()
            ), redirect_stdout(out), redirect_stderr(err):
                rc = loop_main(
                    [
                        "--channel",
                        "telegram",
                        "--user",
                        "w8",
                        "--new-session",
                        "--message",
                        "hi",
                        "--complete",
                        "--provider",
                        "echo",
                        "--reasoning",
                        "off",
                        "--deliver",
                        "buffered",
                    ]
                )
        self.assertEqual(rc, 0, err.getvalue())
        self.assertTrue(seen)
        for req in seen:
            self.assertEqual(req.extra.get("reasoning"), {"enabled": False})


class TestW8EarlyStatus(unittest.TestCase):
    def test_status_sideband_precedes_provider_call(self):
        class MarkerProvider:
            def complete(self, req):
                print("PROVIDER-CALLED", file=sys.stderr)
                return CompletionResult(text="ok")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "AGENTS_HOST_DIR": str(root / "host"),
                "AGENTS_IDENTITY_PATH": str(root / "identity.json"),
                "AGENTS_TRACES_DIR": str(root / "traces"),
                "AGENTS_NO_UPDATE_CHECK": "1",
                "AGENTS_PROJECT_CONTEXT": "0",
                "AGENTS_MEMORY_RULES": "0",
                "AGENTS_CORDIS_EXPOSE": "0",
                "AGENTS_WORKSPACE": "",
            }
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, env, clear=False), patch(
                "runner.loop.get_provider", return_value=MarkerProvider()
            ), redirect_stdout(out), redirect_stderr(err):
                rc = loop_main(
                    [
                        "--channel",
                        "telegram",
                        "--user",
                        "w8",
                        "--new-session",
                        "--message",
                        "hi",
                        "--complete",
                        "--provider",
                        "echo",
                        "--deliver",
                        "buffered",
                    ]
                )
        self.assertEqual(rc, 0, err.getvalue())
        lines = err.getvalue().splitlines()
        status_idx = next(
            i
            for i, line in enumerate(lines)
            if line.startswith(SIDEBAND_PREFIX)
            and json.loads(line[len(SIDEBAND_PREFIX):]).get("type") == "status"
        )
        provider_idx = lines.index("PROVIDER-CALLED")
        self.assertLess(status_idx, provider_idx)
        event = json.loads(lines[status_idx][len(SIDEBAND_PREFIX):])
        self.assertEqual(event["text"], "loading context...")


class TestW8CatalogSnapshot(unittest.TestCase):
    def _probe_env(self, root: Path, overlay: Path, helper: Path):
        pythonpath = str(helper)
        existing = os.environ.get("PYTHONPATH", "")
        if existing:
            pythonpath += os.pathsep + existing
        return {
            "AGENTS_HOST_DIR": str(root / "host"),
            "AGENTS_WORKSPACE": "",
            "AGENTS_MODULES_DIR": str(overlay),
            "AGENTS_CORDIS_EXPOSE": "0",
            "AGENTS_CORDIS_PKGS": "",
            "AGENTS_MCP_CONFIG_FILE": "",
            "AGENTS_MCP_CONFIG": "",
            "AGENTS_MCP_CACHE_FILE": "",
            "AGENTS_MCP_CACHE": "",
            "AGENTS_NO_UPDATE_CHECK": "1",
            "PYTHONPATH": pythonpath,
        }

    def _write_probe(self, overlay: Path, helper: Path) -> Path:
        helper.mkdir(parents=True, exist_ok=True)
        (helper / "w8probe.py").write_text(
            "import json\n"
            "print(json.dumps({'commands': {'run': {'side_effects': 'read-only', "
            "'parameters': {'type': 'object', 'properties': {}}}}}))\n",
            encoding="utf-8",
        )
        overlay.mkdir(parents=True, exist_ok=True)
        manifest = {
            "name": "mcp.w8probe",
            "kind": "mcp",
            "when": "on_request",
            "cadence": "on_request",
            "verb": "python -m w8probe",
            "rests_on": "w8 cache probe",
            "expected_exit": 0,
            "mutates": False,
            "as_tool": True,
            "commands_from": "help-json",
            "pin": True,
            "parameters": {"type": "object", "properties": {}},
        }
        path = overlay / "mcp.w8probe.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_warm_snapshot_and_prompt_assembly_spawn_zero_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            overlay = root / "overlay"
            helper = root / "helper"
            manifest_path = self._write_probe(overlay, helper)
            env = self._probe_env(root, overlay, helper)
            real_run = helpjson.subprocess.run
            with patch.dict(os.environ, env, clear=False):
                catalog.reset_cache()
                helpjson.clear_help_cache()
                empty_package_catalog = root / "empty-package-catalog"
                empty_package_catalog.mkdir()
                with patch.object(modules, "_AUTO_PKGS", ()), patch.object(
                    modules, "MODULES_DIR", empty_package_catalog
                ), patch(
                    "runner.helpjson.subprocess.run", wraps=real_run
                ) as run:
                    rows = list_modules()
                    self.assertIn("mcp.w8probe", {row["name"] for row in rows})
                    self.assertEqual(run.call_count, 1)
                    self.assertTrue(catalog.catalog_cache_path().is_file())

                    catalog.reset_cache()
                    helpjson.clear_help_cache()
                    run.reset_mock()
                    rows = list_modules()
                    block = pinned_verbs_prompt_block()
                    self.assertEqual(run.call_count, 0)
                    row = next(row for row in rows if row["name"] == "mcp.w8probe")
                    self.assertIn("run", row.get("_prompt_commands") or {})
                    self.assertIn("mcp.w8probe", block)

                    run.reset_mock()
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["rests_on"] = "w8 cache probe invalidated"
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    catalog.reset_cache()
                    rows = list_modules()
                    self.assertEqual(run.call_count, 1)
                    row = next(row for row in rows if row["name"] == "mcp.w8probe")
                    self.assertEqual(row["rests_on"], "w8 cache probe invalidated")
        catalog.reset_cache()
        helpjson.clear_help_cache()

    def test_fingerprint_tracks_mcp_and_active_skills_mtimes(self):
        from agents_workspace import Host

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = Host.open(root / "host")
            ws = host.create("w8")
            module_dir = root / "modules"
            module_dir.mkdir()
            env = {
                "AGENTS_HOST_DIR": str(host.dir),
                "AGENTS_WORKSPACE": "w8",
                "AGENTS_MODULES_DIR": str(module_dir),
                "AGENTS_SKILLS_DIR": "",
                "AGENTS_SKILLS_EXTRA": "",
            }
            with patch.dict(os.environ, env, clear=False):
                fp0 = catalog_fingerprint([module_dir], workspace=ws)
                host.mcp_json.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
                fp1 = catalog_fingerprint([module_dir], workspace=ws)
                self.assertNotEqual(fp0, fp1)

                active_root = ws.dir / "active_skills.json"
                active_root.write_text("[]", encoding="utf-8")
                fp2 = catalog_fingerprint([module_dir], workspace=ws)
                self.assertNotEqual(fp1, fp2)

                ws.skills.mkdir(parents=True, exist_ok=True)
                active_skills = ws.skills / "active_skills.json"
                active_skills.write_text("[]", encoding="utf-8")
                fp3 = catalog_fingerprint([module_dir], workspace=ws)
                self.assertNotEqual(fp2, fp3)


if __name__ == "__main__":
    unittest.main()
