"""clarify tool: structured question ends the turn with options for buttons."""

import unittest

from runner.cordis_tools import is_cordis_tool, openai_cordis_tools, run_cordis_tool


def _clarify(args: dict):
    return run_cordis_tool("clarify", {"function": {"arguments": args}})


class TestClarify(unittest.TestCase):
    def test_advertised_tool(self):
        self.assertTrue(is_cordis_tool("clarify"))
        names = {t["function"]["name"] for t in openai_cordis_tools()}
        self.assertIn("clarify", names)

    def test_question_and_options(self):
        out = _clarify({"question": "Which repo?", "options": ["a", "b", " "]})
        self.assertTrue(out.success)
        self.assertEqual(out.clarify["question"], "Which repo?")
        self.assertEqual(out.clarify["options"], ["a", "b"])

    def test_options_optional(self):
        out = _clarify({"question": "What next?"})
        self.assertEqual(out.clarify["options"], [])

    def test_empty_question_fails(self):
        out = _clarify({"question": "  "})
        self.assertFalse(out.success)
        self.assertIsNone(out.clarify)


if __name__ == "__main__":
    unittest.main()
