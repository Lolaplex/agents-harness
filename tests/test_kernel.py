import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from runner.iterm import ParseError, desugar, parse
from runner.kernel import CheckError, alphabet, check, reduce_term, specialize


class _MailSink(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true,"mail":[]}')

    def log_message(self, *_a) -> None:
        pass


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
        http = HTTPServer(("127.0.0.1", 0), _MailSink)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (http.shutdown(), http.server_close()))
        saved = {k: os.environ.get(k) for k in ("RELAY_URL", "RELAY_PORT", "GATEWAY_URL")}
        os.environ["RELAY_URL"] = f"http://127.0.0.1:{http.server_port}"
        os.environ.pop("RELAY_PORT", None)
        os.environ.pop("GATEWAY_URL", None)
        try:
            result = reduce_term(term)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.jobs, ["skill.catalog", "a2a.peer"])


if __name__ == "__main__":
    unittest.main()
