"""Hard-rules system prefix: soft dep, cache, disable switch."""
from __future__ import annotations

from unittest import mock

import pytest

from runner import memory_rules as mr


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    mr.clear_cache()
    monkeypatch.delenv("AGENTS_MEMORY_RULES", raising=False)
    yield
    mr.clear_cache()


def test_disabled_env(monkeypatch):
    monkeypatch.setenv("AGENTS_MEMORY_RULES", "0")
    with mock.patch.object(mr, "_via_import", return_value="<memory_rules>\n- x\n</memory_rules>"):
        assert mr.memory_rules_prompt_block() == ""


def test_import_path_prefixes(monkeypatch):
    block = "<memory_rules>\n- Keep secrets local\n</memory_rules>"
    with mock.patch.object(mr, "_via_import", return_value=block):
        with mock.patch.object(mr, "_via_cli") as cli:
            out = mr.memory_rules_prompt_block()
            cli.assert_not_called()
    assert "Hard rules from local agent memory" in out
    assert block in out
    # cached
    with mock.patch.object(mr, "_via_import") as imp:
        again = mr.memory_rules_prompt_block()
        imp.assert_not_called()
    assert again == out


def test_cli_fallback_when_import_missing(monkeypatch):
    block = "<memory_rules>\n- via cli\n</memory_rules>"
    with mock.patch.object(mr, "_via_import", return_value=None):
        with mock.patch.object(mr, "_via_cli", return_value=block) as cli:
            out = mr.memory_rules_prompt_block("demo")
            cli.assert_called_once_with("demo")
    assert block in out


def test_soft_fail_empty(monkeypatch):
    with mock.patch.object(mr, "_via_import", return_value=None):
        with mock.patch.object(mr, "_via_cli", return_value=""):
            assert mr.memory_rules_prompt_block() == ""


def test_augment_system_includes_rules(monkeypatch):
    from runner.loop import _augment_system
    from runner.fence import FENCE_SYSTEM_NOTE

    block = "<memory_rules>\n- Be brief\n</memory_rules>"
    monkeypatch.setattr(mr, "render_memory_rules", lambda project="", refresh=False: block)
    # Also silence skills
    monkeypatch.setattr("runner.loop.skills_prompt_block", lambda: "")
    out = _augment_system("You are test.", project="")
    assert "You are test." in out
    assert FENCE_SYSTEM_NOTE in out
    assert "Be brief" in out


def test_calendar_update_delete_argv():
    from runner.cordis_tools import _arguments_to_argv

    args = _arguments_to_argv(
        {"name": "mcp.calendar.update"},
        {"href": "/ev", "etag": '"1"', "summary": "Hi"},
    )
    assert args[:4] == ["--href", "/ev", "--etag", '"1"']
    assert "--summary" in args

    deleted = _arguments_to_argv(
        {"name": "mcp.calendar.delete"},
        {"href": "/ev", "etag": "x"},
    )
    assert deleted == ["--href", "/ev", "--etag", "x"]
    assert _arguments_to_argv({"name": "mcp.calendar.calendars"}, {}) == []
