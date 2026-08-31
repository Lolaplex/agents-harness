import io
import unittest

from runner.delivery import bind_delivery, delivery_for
from runner.providers import consume_sse


class TestDelivery(unittest.TestCase):
    def test_telegram_is_buffered(self):
        self.assertEqual(delivery_for("telegram", stdout_tty=True), "buffered")
        self.assertEqual(delivery_for("local", stdout_tty=True), "stream")
        self.assertEqual(delivery_for("local", stdout_tty=False), "buffered")
        self.assertEqual(delivery_for("sandbox", stdout_tty=True), "buffered")
        self.assertEqual(
            delivery_for("telegram", stdout_tty=True, override="stream"), "stream"
        )

    def test_buffered_acks_stderr_and_prints_once(self):
        out, err = io.StringIO(), io.StringIO()
        mode, on_status, on_delta, emit = bind_delivery(
            "telegram", stdout=out, stderr=err, stdout_tty=True
        )
        self.assertEqual(mode, "buffered")
        self.assertIsNone(on_delta)
        on_status("thinking...")
        on_status("still going")
        emit("pong")
        self.assertEqual(out.getvalue(), "pong\n")
        self.assertEqual(err.getvalue(), "thinking...\n")

    def test_stream_writes_tokens(self):
        out, err = io.StringIO(), io.StringIO()
        mode, on_status, on_delta, emit = bind_delivery(
            "local", stdout=out, stderr=err, stdout_tty=True
        )
        self.assertEqual(mode, "stream")
        on_status("thinking...")
        on_delta("sand")
        on_delta("box")
        emit("sandbox")
        self.assertEqual(out.getvalue(), "sandbox\n")
        self.assertEqual(err.getvalue(), "thinking...\n")

    def test_consume_sse_forwards_deltas(self):
        pieces = []
        lines = [
            'data: {"choices":[{"delta":{"content":"a"}}]}',
            'data: {"choices":[{"delta":{"content":"b"}}]}',
            "data: [DONE]",
        ]
        text, _, _ = consume_sse(lines, on_delta=pieces.append)
        self.assertEqual(text, "ab")
        self.assertEqual(pieces, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
