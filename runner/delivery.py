"""How a channel shows generation. The HTTP side always streams.

TTY can write tokens. Telegram and other push channels buffer the answer and
only ack that the prompt was received (thinking / typing).
"""

from __future__ import annotations

import sys
from typing import Callable, TextIO

BUFFERED_CHANNELS = frozenset({"telegram", "http", "gateway"})
STREAM_OK_CHANNELS = frozenset({"local", "cli", "tty"})


def delivery_for(channel: str, *, stdout_tty: bool | None = None, override: str = "") -> str:
    """Return 'buffered' or 'stream'."""
    if override in ("buffered", "stream"):
        return override
    ch = (channel or "").strip().lower()
    if ch in BUFFERED_CHANNELS:
        return "buffered"
    tty = sys.stdout.isatty() if stdout_tty is None else stdout_tty
    if ch in STREAM_OK_CHANNELS and tty:
        return "stream"
    return "buffered"


def bind_delivery(
    channel: str,
    *,
    override: str = "",
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdout_tty: bool | None = None,
) -> tuple[str, Callable[[str], None] | None, Callable[[str], None] | None, Callable[[str], None]]:
    """status, delta, emit for one request.

    status: prompt received, generation ongoing (stderr).
    delta: tokens, only when delivery is stream (stdout, no newline).
    emit: final buffered answer (stdout). Stream delivery only ends the line.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    mode = delivery_for(channel, stdout_tty=stdout_tty, override=override)
    def on_status(text: str) -> None:
        msg = (text or "thinking...").strip()
        if msg:
            print(msg, file=err, flush=True)

    on_delta: Callable[[str], None] | None = None
    if mode == "stream":

        def on_delta(piece: str) -> None:
            out.write(piece)
            out.flush()

        def emit(_text: str) -> None:
            out.write("\n")
            out.flush()

    else:

        def emit(text: str) -> None:
            if text:
                print(text, file=out, flush=True)

    return mode, on_status, on_delta, emit
