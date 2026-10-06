"""Unit tests for the non-blocking PyPI update check (no network)."""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from runner import updates as u

PKG = "agents-harness"
CUR = "1.0.0"


class UpdateCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name) / "agents"
        self._old = {k: os.environ.get(k) for k in (
            "AGENTS_HOME", "AGENTS_NO_UPDATE_CHECK", "CI", "GITHUB_ACTIONS", "XDG_CACHE_HOME"
        )}
        os.environ["AGENTS_HOME"] = str(self.home)
        for k in ("AGENTS_NO_UPDATE_CHECK", "CI", "GITHUB_ACTIONS", "XDG_CACHE_HOME"):
            os.environ.pop(k, None)
        u._mcp_checked.clear()

    def tearDown(self) -> None:
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmpdir.cleanup()

    def _write_cache(self, latest: str) -> None:
        path = u._cache_file(PKG)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"last_checked": int(time.time()), "latest_version": latest}),
            encoding="utf-8",
        )

    def test_newer_prints_stderr(self) -> None:
        self._write_cache("1.2.0")
        buf = io.StringIO()
        with mock.patch.object(u.sys, "stderr", buf), mock.patch.object(
            u.urllib.request, "urlopen"
        ) as urlopen:
            u.check_for_updates(PKG, CUR)
            urlopen.assert_not_called()
        self.assertEqual(
            buf.getvalue(),
            f"{PKG} 1.2.0 available (you have {CUR}): uv tool upgrade {PKG}\n",
        )

    def test_same_version_silent(self) -> None:
        self._write_cache(CUR)
        buf = io.StringIO()
        with mock.patch.object(u.sys, "stderr", buf):
            u.check_for_updates(PKG, CUR)
        self.assertEqual(buf.getvalue(), "")

    def test_offline_silent(self) -> None:
        buf = io.StringIO()
        with mock.patch.object(u.sys, "stderr", buf), mock.patch.object(
            u.urllib.request, "urlopen", side_effect=OSError("offline")
        ):
            u.check_for_updates(PKG, CUR)
        self.assertEqual(buf.getvalue(), "")
        self.assertFalse(u._cache_file(PKG).exists())

    def test_disabled_env(self) -> None:
        os.environ["AGENTS_NO_UPDATE_CHECK"] = "1"
        self._write_cache("9.9.9")
        buf = io.StringIO()
        with mock.patch.object(u.sys, "stderr", buf):
            u.check_for_updates(PKG, CUR)
        self.assertEqual(buf.getvalue(), "")

    def test_ci_env_silent(self) -> None:
        os.environ["CI"] = "true"
        self._write_cache("9.9.9")
        buf = io.StringIO()
        with mock.patch.object(u.sys, "stderr", buf):
            u.check_for_updates(PKG, CUR)
        self.assertEqual(buf.getvalue(), "")

    def test_fetches_and_caches(self) -> None:
        payload = json.dumps({"info": {"version": "2.0.0"}}).encode()
        resp = mock.MagicMock()
        resp.read.return_value = payload
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = None
        buf = io.StringIO()
        with mock.patch.object(u.sys, "stderr", buf), mock.patch.object(
            u.urllib.request, "urlopen", return_value=resp
        ) as urlopen:
            u.check_for_updates(PKG, CUR)
            urlopen.assert_called_once()
        self.assertIn("2.0.0 available", buf.getvalue())
        cached = json.loads(u._cache_file(PKG).read_text(encoding="utf-8"))
        self.assertEqual(cached["latest_version"], "2.0.0")

    def test_mcp_notice_once(self) -> None:
        self._write_cache("3.0.0")
        first = u.mcp_update_notice(PKG, CUR)
        second = u.mcp_update_notice(PKG, CUR)
        self.assertEqual(
            first, f"{PKG} 3.0.0 available (you have {CUR}): uv tool upgrade {PKG}"
        )
        self.assertEqual(second, "")

    def test_stdout_untouched(self) -> None:
        self._write_cache("9.0.0")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(u.sys, "stdout", out), mock.patch.object(u.sys, "stderr", err):
            u.check_for_updates(PKG, CUR)
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(err.getvalue())


if __name__ == "__main__":
    unittest.main()
