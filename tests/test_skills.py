"""skill.list / skill.load read SKILL.md files."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from runner.cordis_tools import handle_cordis_tool
from runner.skills import list_skills, load_skill, skills_prompt_block


class TestSkills(unittest.TestCase):
    def test_list_and_load_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "demo"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Say hello\n---\n# Demo\nDo the thing.\n",
                encoding="utf-8",
            )
            extra = root / "extra" / "other"
            extra.mkdir(parents=True)
            (extra / "SKILL.md").write_text(
                "---\nname: other\ndescription: Second skill\n---\nbody\n",
                encoding="utf-8",
            )
            env = {
                "AGENTS_SKILLS_DIR": str(root),
                "AGENTS_SKILLS_EXTRA": str(root / "extra"),
            }
            old = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                rows = list_skills()
                names = {row["name"] for row in rows}
                self.assertEqual(names, {"demo", "other"})
                self.assertIn("demo: Say hello", skills_prompt_block())
                self.assertIn("Do the thing.", load_skill("demo") or "")
                listed = handle_cordis_tool(
                    "call_job",
                    {"function": {"arguments": json.dumps({"name": "skill.list"})}},
                )
                self.assertIn("demo", listed)
                self.assertIn("Say hello", listed)
                loaded = handle_cordis_tool(
                    "call_job",
                    {
                        "function": {
                            "arguments": json.dumps(
                                {"name": "skill.load", "arguments": {"name": "other"}}
                            )
                        }
                    },
                )
                self.assertIn("body", loaded)
                alias = handle_cordis_tool(
                    "call_job",
                    {"function": {"arguments": json.dumps({"name": "skill.catalog"})}},
                )
                self.assertIn("other", alias)
                missing = handle_cordis_tool(
                    "call_job",
                    {
                        "function": {
                            "arguments": json.dumps(
                                {"name": "skill.load", "arguments": {"name": "missing"}}
                            )
                        }
                    },
                )
                self.assertIn("unknown skill", missing)
            finally:
                for key, val in old.items():
                    if val is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = val


if __name__ == "__main__":
    unittest.main()
