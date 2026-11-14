"""The recall eval distinguishes using or searching from asking."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from runner.recall_eval import builtin_rows, judge, main, score_transcript


class RecallEval(unittest.TestCase):
    def test_labels(self):
        used = judge(fact="ochre", reply="The canary is ochre.", searched=False, info_exists=True)
        self.assertEqual(used.label, "used")
        self.assertTrue(used.passed)
        searched = judge(
            fact="ochre",
            reply="I checked the notes.",
            searched=True,
            info_exists=True,
        )
        self.assertEqual(searched.label, "searched")
        asked = judge(
            fact="ochre",
            reply="Could you tell me the color?",
            searched=False,
            info_exists=True,
        )
        self.assertEqual(asked.label, "asked")
        self.assertFalse(asked.passed)
        missing = judge(fact="", reply="Could you tell me?", searched=False, info_exists=False)
        self.assertEqual(missing.label, "ask_ok")
        self.assertTrue(missing.passed)

    def test_builtin_rows_match_their_expected_labels(self):
        rows = builtin_rows()
        self.assertTrue(rows)
        self.assertTrue(all(row.judgment.label == row.expect for row in rows))
        self.assertEqual(main([]), 0)

    def test_transcript_of_an_ask_fails(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "turns.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "id": "canary-color",
                            "reply": "Could you tell me the color?",
                            "searched": False,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            judgments, errors = score_transcript(str(path))
            self.assertEqual(judgments[0].label, "asked")
            self.assertTrue(errors)
            self.assertEqual(main(["--transcript", str(path)]), 1)
