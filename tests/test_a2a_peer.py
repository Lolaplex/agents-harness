"""a2a.peer relay URL resolution and failure honesty."""

import contextlib
import io
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from runner import a2a_peer

_KEYS = ("RELAY_URL", "RELAY_PORT", "GATEWAY_URL", "RELAY_SECRET", "GATEWAY_SECRET", "A2A_BODY")


def _clean_env() -> dict:
    saved = {k: os.environ.get(k) for k in _KEYS}
    for k in _KEYS:
        os.environ.pop(k, None)
    return saved


def _restore(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class _Sink(BaseHTTPRequestHandler):
    hits: list = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        _Sink.hits.append(
            {
                "path": self.path,
                "secret": self.headers.get("X-Relay-Secret") or "",
                "body": json.loads(self.rfile.read(length) or b"{}"),
            }
        )
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true,"mail":[]}')

    def log_message(self, *_a) -> None:
        pass


class TestUrlResolution(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = _clean_env()

    def tearDown(self) -> None:
        _restore(self._saved)

    def test_relay_url_wins(self) -> None:
        os.environ["RELAY_URL"] = "http://relay:9000/"
        os.environ["GATEWAY_URL"] = "http://gw:1"
        self.assertEqual(a2a_peer.relay_url(), "http://relay:9000")

    def test_relay_port(self) -> None:
        os.environ["RELAY_PORT"] = "8000"
        self.assertEqual(a2a_peer.relay_url(), "http://127.0.0.1:8000")

    def test_gateway_fallback(self) -> None:
        os.environ["GATEWAY_URL"] = "http://gw:7000"
        self.assertEqual(a2a_peer.relay_url(), "http://gw:7000")

    def test_default(self) -> None:
        self.assertEqual(a2a_peer.relay_url(), "http://127.0.0.1:8787")


class TestA2aPeer(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = _clean_env()
        _Sink.hits = []
        self._http = HTTPServer(("127.0.0.1", 0), _Sink)
        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self._http.shutdown(), self._http.server_close()))

    def tearDown(self) -> None:
        _restore(self._saved)

    def _run(self, argv=()) -> tuple[int, dict]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = a2a_peer.main(list(argv))
        out = buf.getvalue().strip()
        return code, json.loads(out) if out else {}

    def test_success_posts_body_and_secret(self) -> None:
        os.environ["RELAY_URL"] = f"http://127.0.0.1:{self._http.server_port}"
        os.environ["RELAY_SECRET"] = "sek"
        code, out = self._run(['{"verb":"mail.send","to":"did:key:abc"}'])
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        hit = _Sink.hits[0]
        self.assertEqual(hit["path"], "/v1/a2a/mail")
        self.assertEqual(hit["secret"], "sek")
        self.assertEqual(hit["body"]["verb"], "mail.send")

    def test_unreachable_is_not_ok(self) -> None:
        os.environ["RELAY_URL"] = "http://127.0.0.1:1"  # nothing listens
        code, out = self._run(['{"verb":"mail.get"}'])
        self.assertNotEqual(code, 0)
        self.assertFalse(out.get("ok", True))
        self.assertNotIn("stub", out)

    def test_default_body(self) -> None:
        os.environ["RELAY_URL"] = f"http://127.0.0.1:{self._http.server_port}"
        code, _ = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(_Sink.hits[0]["body"], {"verb": "mail.get"})

    def test_bad_json_exits_2(self) -> None:
        code, out = self._run(["{nope"])
        self.assertEqual(code, 2)
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
