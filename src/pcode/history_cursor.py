"""Process-local continuations and bounded, snapshot journal reads for recall.

Continuations retain parser state, not a reusable search index. They expire and
never write transcript text to disk. Journal readers reopen at their byte offset
on each page, so no file descriptor is held while a caller considers a result.
"""

import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pcode.sessions import SessionInfo, SessionReadBudget

CURSOR_TTL = 30 * 60
MAX_CURSORS = 16


@dataclass
class Continuation:
    binding: tuple
    pages: Generator[tuple[Any, bool], None, None]
    expires: float


_continuations: OrderedDict[str, Continuation] = OrderedDict()
_lock = threading.Lock()


def advance(
    binding: tuple,
    after: str | None,
    pages: Callable[[], Generator[tuple[Any, bool], None, None]],
):
    """Advance a single-use cursor, returning the page and its next opaque token."""
    now = time.monotonic()
    with _lock:
        for token, saved in list(_continuations.items()):
            if saved.expires <= now:
                del _continuations[token]
                saved.pages.close()
        if after:
            saved = _continuations.get(after)
            if saved is None:
                raise ValueError("Unknown cursor or expired continuation; restart without after.")
            if saved.binding != binding:
                raise ValueError("Cursor does not match this scope or read request.")
            del _continuations[after]
            iterator = saved.pages
        else:
            iterator = pages()
    try:
        result, more = next(iterator)
    except BaseException:
        iterator.close()
        raise
    token = None
    if more:
        token = uuid4().hex
        with _lock:
            while len(_continuations) >= MAX_CURSORS:
                _, saved = _continuations.popitem(last=False)
                saved.pages.close()
            _continuations[token] = Continuation(binding, iterator, time.monotonic() + CURSOR_TTL)
    else:
        iterator.close()
    return result, token


@dataclass
class JournalReader:
    """Read a finite journal snapshot, even when a JSON line spans several pages."""

    info: SessionInfo
    root: Path
    kinds: tuple[str, ...]
    offset: int = 0
    pending: bytearray = field(default_factory=bytearray)
    incomplete_tail: bool = False

    def __post_init__(self):
        self.path = self.root / self.info.id / "transcript.jsonl"
        self._check_path()
        stat = self.path.stat()
        self.identity = (stat.st_dev, stat.st_ino)
        self.size = stat.st_size
        self.mtime = stat.st_mtime_ns
        self.markers = tuple(f'"{kind}"'.encode() for kind in self.kinds)

    @property
    def done(self) -> bool:
        return self.offset == self.size

    def _check_path(self):
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise OSError("symlinked session")
        if not (self.path.parent / "session.json").is_file():
            raise OSError("session removed")

    def validate(self, stat=None):
        self._check_path()
        stat = stat or self.path.stat()
        if (
            (stat.st_dev, stat.st_ino) != self.identity
            or stat.st_size < self.size
            or (stat.st_size == self.size and stat.st_mtime_ns != self.mtime)
        ):
            raise ValueError("Session journal changed; restart without after.")

    def records(self, budget: SessionReadBudget):
        self.validate()
        with self.path.open("rb") as stream:
            self.validate(os.fstat(stream.fileno()))
            stream.seek(self.offset)
            while not self.done and budget.remaining > 0:
                part = stream.readline(min(budget.remaining, self.size - self.offset))
                if not part:
                    raise ValueError("Session journal was truncated; restart without after.")
                budget.remaining -= len(part)
                self.offset += len(part)
                self.pending.extend(part)
                terminated = part.endswith(b"\n")
                if not terminated and not self.done:
                    continue
                line, self.pending = self.pending, bytearray()
                if terminated and not any(marker in line for marker in self.markers):
                    continue
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    # A complete final JSON object needs no newline, but a torn
                    # live write must not be mistaken for complete coverage.
                    if not terminated:
                        self.incomplete_tail = True
                    continue
                if isinstance(record, dict) and record.get("kind") in self.kinds:
                    yield record
        budget.exhausted = not self.done
