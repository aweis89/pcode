"""Return startup probe bytes through prompt_toolkit's normal input callback."""

import asyncio
from contextlib import contextmanager

from prompt_toolkit.input.vt100 import Vt100Input
from prompt_toolkit.input.vt100_parser import Vt100Parser


class BackgroundReplyParser(Vt100Parser):
    """Discard one delayed OSC 11 reply, even when split across terminal reads."""

    _header = "\x1b]11;"

    def __init__(self, callback):
        super().__init__(callback)
        self.pending = ""
        self.waiting = True

    def feed(self, data: str) -> None:
        if not self.waiting:
            super().feed(data)
            return
        self.pending += data
        start = self.pending.find(self._header)
        if start < 0:
            # Hold only a possible split header, never ordinary typing.
            keep = next(
                (
                    n
                    for n in range(len(self._header) - 1, 0, -1)
                    if self.pending.endswith(self._header[:n])
                ),
                0,
            )
            text = self.pending[:-keep] if keep else self.pending
            self.pending = self.pending[-keep:] if keep else ""
            super().feed(text)
            return
        super().feed(self.pending[:start])
        self.pending = self.pending[start:]
        bell = self.pending.find("\x07")
        st = self.pending.find("\x1b\\")
        endings = [end for end in (bell + 1 if bell >= 0 else 0, st + 2 if st >= 0 else 0) if end]
        if endings:
            end = min(endings)
            from pcode.theme import background_theme

            if background_theme(self.pending[:end].encode()) is None:
                super().feed(self.pending[:end])
            tail, self.pending = self.pending[end:], ""
            self.waiting = False
            super().feed(tail)
        elif len(self.pending) > 128:
            # A malformed response must not hold subsequent typing indefinitely.
            text, self.pending = self.pending, ""
            self.waiting = False
            super().feed(text)

    def flush(self) -> None:
        text, self.pending = self.pending, ""
        # An incomplete terminal reply must not hold the next prompt hostage.
        # Bare Escape is still a key and must reach the ordinary parser.
        if not text.startswith("\x1b]"):
            super().feed(text)
        super().flush()


class StartupInput(Vt100Input):
    def __init__(self, stdin, pending: bytes, awaiting_reply: bool):
        super().__init__(stdin)
        self.pending = pending
        if awaiting_reply:
            self.vt100_parser = BackgroundReplyParser(lambda key: self._buffer.append(key))

    @contextmanager
    def attach(self, input_ready_callback):
        with super().attach(input_ready_callback):
            if self.pending:
                # Use the application's callback so it also schedules its normal
                # escape timeout. Feeding keys in pre_run would bypass that timer.
                asyncio.get_running_loop().call_soon(input_ready_callback)
            yield

    def read_keys(self):
        if self.pending:
            # Keep the decoder shared with later reads, including split UTF-8.
            text = self.stdin_reader._stdin_decoder.decode(self.pending)
            self.pending = b""
            self.vt100_parser.feed(text)
        return super().read_keys()
