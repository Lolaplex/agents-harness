"""Tool-output cap: unchanged under the limit, head and tail over it."""

from __future__ import annotations

import os
import re
import unittest
from unittest.mock import patch

from runner.executor import (
    DEFAULT_TOOL_OUTPUT_MAX,
    clip_tool_output,
    present_job_result,
    tool_output_max,
)

_MARKER = re.compile(r"\[\.\.\. (\d+) chars omitted \.\.\.\]")


def _assert_middle_clip(test: unittest.TestCase, text: str, limit: int, out: str) -> None:
    test.assertLessEqual(len(out), limit)
    test.assertNotEqual(out, text)
    match = _MARKER.search(out)
    test.assertIsNotNone(match, out)
    assert match is not None
    omitted = int(match.group(1))
    marker = match.group(0)
    head, tail = out.split(marker, 1)
    test.assertEqual(text[: len(head)], head)
    if tail:
        test.assertEqual(text[-len(tail) :], tail)
    else:
        test.assertEqual(tail, "")
    test.assertEqual(len(text) - len(head) - len(tail), omitted)
    test.assertGreater(omitted, 0)


class TestToolOutputCap(unittest.TestCase):
    def test_default_cap(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENTS_TOOL_OUTPUT_MAX", None)
            self.assertEqual(tool_output_max(), DEFAULT_TOOL_OUTPUT_MAX)
            self.assertEqual(DEFAULT_TOOL_OUTPUT_MAX, 32768)

    def test_under_cap_unchanged(self) -> None:
        text = "HEAD" + ("x" * 40) + "TAIL"
        self.assertEqual(clip_tool_output(text, limit=len(text)), text)
        self.assertEqual(clip_tool_output(text, limit=len(text) + 25), text)
        self.assertEqual(clip_tool_output("", limit=10), "")

    def test_over_cap_keeps_head_tail_and_marker(self) -> None:
        text = "HEAD" + ("m" * 5000) + "TAIL"
        out = clip_tool_output(text, limit=180)
        _assert_middle_clip(self, text, 180, out)
        self.assertTrue(out.startswith("HEAD"))
        self.assertTrue(out.endswith("TAIL"))
        self.assertNotEqual(out, text[-180:])

    def test_env_override(self) -> None:
        text = "HEAD" + ("m" * 400) + "TAIL"
        with patch.dict(os.environ, {"AGENTS_TOOL_OUTPUT_MAX": "96"}, clear=False):
            self.assertEqual(tool_output_max(), 96)
            self.assertEqual(clip_tool_output("short"), "short")
            out = clip_tool_output(text)
        _assert_middle_clip(self, text, 96, out)
        self.assertTrue(out.startswith("HEAD"))
        self.assertTrue(out.endswith("TAIL"))

        larger = "HEAD" + ("m" * 40) + "TAIL"
        with patch.dict(os.environ, {"AGENTS_TOOL_OUTPUT_MAX": "10000"}, clear=False):
            self.assertEqual(clip_tool_output(larger), larger)

        with patch.dict(os.environ, {"AGENTS_TOOL_OUTPUT_MAX": "nope"}, clear=False):
            self.assertEqual(tool_output_max(), DEFAULT_TOOL_OUTPUT_MAX)
        with patch.dict(os.environ, {"AGENTS_TOOL_OUTPUT_MAX": "-5"}, clear=False):
            self.assertEqual(tool_output_max(), DEFAULT_TOOL_OUTPUT_MAX)

    def test_clip_invariants_across_sizes(self) -> None:
        for length, limit in (
            (80, 40),
            (200, 48),
            (999, 64),
            (1000, 80),
            (10000, 120),
            (32769, 32768),
        ):
            text = "S" + ("a" * (length - 2)) + "E"
            out = clip_tool_output(text, limit=limit)
            if len(text) <= limit:
                self.assertEqual(out, text)
            else:
                _assert_middle_clip(self, text, limit, out)

    def test_tiny_cap_stays_within_limit(self) -> None:
        out = clip_tool_output("S" + ("a" * 40) + "E", limit=10)
        self.assertLessEqual(len(out), 10)
        self.assertTrue(out.startswith("["), out)

    def test_present_job_result_uses_full_streams(self) -> None:
        full = "HEAD" + ("z" * 2000) + "TAIL"
        err = "ERR" + ("e" * 40)
        shown = present_job_result(
            {
                "status": "SUCCESS",
                "stdout": full,
                "stderr": err,
                "stdout_tail": full[-1000:],
                "stderr_tail": err[-10:],
            }
        )
        self.assertNotIn("stdout", shown)
        self.assertNotIn("stderr", shown)
        self.assertEqual(shown["stdout_tail"], full)
        self.assertEqual(shown["stderr_tail"], err)
        self.assertNotEqual(shown["stdout_tail"], full[-1000:])


if __name__ == "__main__":
    unittest.main()
