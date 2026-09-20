"""Bounded presentation history, independent of model/session persistence."""

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps

# A regenerated view replaces the terminal's scrollback, so the budget has to
# cover what a terminal keeps. Counting entries alone is misleading: one
# committed Markdown block costs two entries, so an entry cap evicts visible
# history long before memory is a concern. Bound retained text instead, well
# past any terminal's scrollback, and keep a loose entry cap so a flood of tiny
# writes cannot grow the deque without limit.
ENTRY_LIMIT = 20_000
CHAR_BUDGET = 2_000_000


@dataclass(frozen=True)
class RetainedMarkdown:
    """Markdown kept as its source: replay rebuilds the renderable and its theme.

    A parsed token tree costs tens of times the text it came from, and replay
    rebuilds it from the source anyway, so retaining it would bound the log by
    Rich's parse output instead of by the transcript.
    """

    markup: str


def snapshot(value):
    """Copy retained arguments so later mutation cannot change replayed history."""
    if isinstance(value, tuple):
        return tuple(snapshot(item) for item in value)
    markup = getattr(value, "markup", None)
    if isinstance(markup, str) and hasattr(value, "parsed"):
        return RetainedMarkdown(markup)
    return deepcopy(value)


def retained_chars(value, depth: int = 0) -> int:
    """Estimate the text one retained argument replays, without rendering it."""
    if isinstance(value, str):
        return len(value)
    if depth >= 4:
        return 0
    if isinstance(value, (tuple, list, set, frozenset)):
        return sum(retained_chars(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return sum(retained_chars(item, depth + 1) for item in value.values())
    markup = getattr(value, "markup", None)
    if isinstance(markup, str):  # Markdown replays from its source, not its tokens.
        return len(markup)
    attributes = getattr(value, "__dict__", None)
    if attributes is None:  # Numbers, None, and slot-only objects carry no text.
        return 0
    return sum(retained_chars(item, depth + 1) for item in attributes.values())


@dataclass
class Entry:
    """One recorded presentation call, with the text it will replay."""

    method: str
    args: tuple
    kwargs: dict
    chars: int = field(init=False)

    def __post_init__(self) -> None:
        self.chars = retained_chars(self.args) + retained_chars(self.kwargs)


class TranscriptLog:
    """Keep recent semantic writes, including writes hidden by display settings."""

    def __init__(self, limit: int = ENTRY_LIMIT, max_chars: int = CHAR_BUDGET):
        self.entries: deque[Entry] = deque()
        self.limit = limit
        self.max_chars = max_chars
        self.chars = 0
        self.dropped = False
        self.recording = True

    def append(self, method, args, kwargs):
        if method == "thinking" and self.entries and self.entries[-1].method == method:
            previous = self.entries.pop()
            self.chars -= previous.chars
            # Both halves are already-copied strings; merging needs no new copy.
            args, kwargs = (previous.args[0] + args[0],), {}
        else:
            args, kwargs = snapshot(args), deepcopy(kwargs)
        entry = Entry(method, args, kwargs)
        self.entries.append(entry)
        self.chars += entry.chars
        # Always keep the newest entry, even when it alone exceeds the budget.
        while len(self.entries) > self.limit or (
            self.chars > self.max_chars and len(self.entries) > 1
        ):
            self.chars -= self.entries.popleft().chars
            self.dropped = True

    def clear(self) -> None:
        """Forget retained history so the next rebuild starts from an empty screen."""
        self.entries.clear()
        self.chars = 0
        self.dropped = False


def recorded(method):
    """Record the outermost presentation operation, never its nested prints."""

    @wraps(method)
    def write(self, *args, **kwargs):
        log = self.log
        if not log.recording:
            return method(self, *args, **kwargs)
        log.append(method.__name__, args, kwargs)
        log.recording = False
        try:
            return method(self, *args, **kwargs)
        finally:
            log.recording = True

    return write
