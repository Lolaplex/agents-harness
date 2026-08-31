import json
import unittest
from pathlib import Path

from runner.iterm import ParseError, desugar, parse
from runner.kernel import CheckError, alphabet, check, reduce_term, specialize


def _cat(*names: str) -> dict:
    return {
        n: {
            "name": n,
            "verb": f"python -c \"print('{n}')\"",
            "cadence": "on_request",
            "rests_on": "test",
            "expected_exit": 0,
        }
        for n in names
    }


def _exec(manifest: dict) -> dict:
    return {"status": "SUCCESS", "job": manifest["name"], "exit_code": 0}


class TestJobKernel(unittest.TestCase):
    def test_unknown_atom_rejected(self):
        term = parse({"op": "atom", "name": "no.such"})
        with self.assertRaises(CheckError):
            check(term, _cat("skill.catalog"))

    def test_seq_runs_first_then_second(self):
        term = parse({"op": "seq", "steps": ["a", "b"]})
        result = reduce_term(term, _cat("a", "b"), execute=_exec)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.jobs, ["a", "b"])

    def test_mix_equation_same_jobs(self):
        term = parse({"op": "seq", "steps": [{"op": "atom", "name": "a"}, "b"]})
        cat = _cat("a", "b")
        via_i = reduce_term(term, cat, execute=_exec, mix=False)
        via_spec = reduce_term(specialize(term), cat, execute=_exec, mix=False)
        self.assertEqual(via_i.jobs, via_spec.jobs)
        self.assertEqual(via_i.jobs, ["a", "b"])

    def test_bundled_example_checks(self):
        path = Path(__file__).resolve().parents[1] / "runner" / "terms" / "skill-then-a2a.json"
        term = parse(json.loads(path.read_text(encoding="utf-8")))
        check(term, alphabet())
        self.assertEqual(term.atoms(), ["skill.catalog", "a2a.peer"])
        self.assertEqual(set(desugar(term).atoms()), {"skill.catalog", "a2a.peer"})

    def test_parse_rejects_bad_op(self):
        with self.assertRaises(ParseError):
            parse({"op": "lambda"})

    def test_live_seq_skill_then_a2a(self):
        path = Path(__file__).resolve().parents[1] / "runner" / "terms" / "skill-then-a2a.json"
        term = parse(json.loads(path.read_text(encoding="utf-8")))
        result = reduce_term(term)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.jobs, ["skill.catalog", "a2a.peer"])


if __name__ == "__main__":
    unittest.main()
