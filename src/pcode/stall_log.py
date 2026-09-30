"""Log event-loop stalls in the terminal: the moments typed keys wait to be read.

Keystrokes and model output share one asyncio loop. When synchronous work holds
it, keys queue in the tty and land together once it frees, so typing seems to
freeze and then a whole word pops up. A heartbeat task notes when the loop last
ran; a watcher thread samples the loop thread's stack while the heartbeat is
overdue and appends one JSON line per stall to `stalls.jsonl` in the state
directory. A freeze with no stall logged at that time means the loop was free
and the terminal itself held the frame back.

Records hold file names, line numbers, and function names, never arguments or
locals, as `pcode.profiling` does.
"""

import asyncio
import gc
import json
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path

THRESHOLD = 0.1
"""Seconds late before a heartbeat counts as a stall; ~100 ms is where typing visibly lags."""
BEAT = 0.05
POLL = 0.02
MAX_BYTES = 1_000_000
"""The log is renamed to `stalls.jsonl.1` past this size, replacing the previous one."""
FRAMES_KEPT = 30
STACKS_KEPT = 3
GC_FRAME = "<garbage collection>"


def stall_log_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "stalls.jsonl"


def _where(frame) -> str:
    path = frame.f_code.co_filename
    for marker in ("site-packages/", "/src/"):
        if marker in path:
            path = path.rsplit(marker, 1)[1]
            break
    else:
        path = os.path.basename(path)
    return f"{path}:{frame.f_lineno} {frame.f_code.co_qualname}"


def _idle(frame) -> bool:
    """Waiting in the selector: the loop is late only because the process was
    suspended (Ctrl+Z, a debugger), not blocked by work."""
    return frame.f_code.co_filename.endswith("selectors.py")


class StallWatch:
    """Run `heartbeat()` as a task on the loop to watch; `stop()` when done."""

    def __init__(self, path: Path | None = None, *, threshold: float = THRESHOLD, context=None):
        self.path = path or stall_log_path()
        self.threshold = threshold
        # Called from the watcher thread when a stall begins; must only read.
        self.context = context
        self._due: float | None = None
        self._ended: tuple[float, float] | None = None  # (due, how late it ran)
        self._loop_thread: int | None = None
        self._in_gc = False
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None

    async def heartbeat(self) -> None:
        self._loop_thread = threading.get_ident()
        if self._watcher is None:
            gc.callbacks.append(self._gc)
            self._watcher = threading.Thread(target=self._watch, name="stall-watch", daemon=True)
            self._watcher.start()
        while True:
            now = time.monotonic()
            if self._due is not None:
                self._ended = (self._due, now - self._due)
            self._due = now + BEAT
            await asyncio.sleep(BEAT)

    def stop(self) -> None:
        self._stop.set()
        if self._watcher is not None:
            self._watcher.join(timeout=1)
            if self._gc in gc.callbacks:
                gc.callbacks.remove(self._gc)

    def _gc(self, phase: str, info: dict) -> None:
        self._in_gc = phase == "start"

    def _watch(self) -> None:
        stalled = None  # The overdue `_due` being sampled.
        samples: Counter[tuple[str, ...]] = Counter()
        context: dict = {}
        while not self._stop.wait(POLL):
            due = self._due
            if stalled is not None and due != stalled:
                ended = self._ended
                late = ended[1] if ended and ended[0] == stalled else time.monotonic() - stalled
                if samples:
                    self._write(late, samples, context)
                stalled, samples = None, Counter()
            if due is None or time.monotonic() < due + self.threshold:
                continue
            frame = sys._current_frames().get(self._loop_thread)
            if frame is None or _idle(frame):
                continue
            if stalled is None:
                stalled, context = due, self._context()
            stack = []
            while frame is not None and len(stack) < FRAMES_KEPT:
                stack.append(_where(frame))
                frame = frame.f_back
            if self._in_gc:
                stack.insert(0, GC_FRAME)
            samples[tuple(stack)] += 1

    def _context(self) -> dict:
        if self.context is None:
            return {}
        try:
            return dict(self.context())
        except Exception:
            return {}

    def _write(self, late: float, samples: Counter, context: dict) -> None:
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - late)),
            "pid": os.getpid(),
            "stall_ms": round(late * 1000),
            "samples": sum(samples.values()),
            **context,
            # Innermost frame first: the top line is what was running.
            "stacks": [
                {"samples": count, "frames": list(stack)}
                for stack, count in samples.most_common(STACKS_KEPT)
            ],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with self.path.open("a") as file:
                file.write(json.dumps(record) + "\n")
        except OSError:
            pass  # Diagnostics must never disturb the session.
