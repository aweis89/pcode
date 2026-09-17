"""Bounded presentation history, independent of model/session persistence."""

from collections import deque
from copy import deepcopy
from functools import wraps


class TranscriptLog:
    """Keep recent semantic writes, including writes hidden by display settings."""

    def __init__(self, limit: int = 2000):
        self.entries = deque(maxlen=limit)
        self.dropped = False
        self.recording = True

    def append(self, method, args, kwargs):
        self.dropped |= len(self.entries) == self.entries.maxlen
        self.entries.append((method, deepcopy(args), deepcopy(kwargs)))


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
