"""Log event-loop stalls in the terminal: the moments typed keys wait to be read.

Keystrokes and model output share one asyncio loop. When synchronous work holds
it, keys queue in the tty and land together once it frees, so typing seems to
freeze and then a whole word pops up. A heartbeat task notes when the loop last
ran; a watcher thread samples the loop thread's stack while the heartbeat is
overdue and appends one JSON line per stall to `stalls.jsonl` in the state
directory. A freeze with no stall logged at that time means the loop was free
and the terminal itself held the frame back.

A stall that holds the GIL throughout (garbage collection, one long C call)
leaves the watcher no chance to sample, so it is logged from the late heartbeat
alone, with no stacks; `gc_ms` says how much of it was collection.

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
BUSY_SHARE = 0.1
"""Process CPU per wall second above which an unsampled stall was work, not a suspended
process (Ctrl+Z, a sleeping laptop), which stops the watcher thread as well. Low,
because a busy process on a loaded machine is still a stall the user feels yet may
get only a fraction of a core (a quarter was measured); a suspended one gets none."""


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
    """The event loop waiting for I/O, so it is late only because the process was
    suspended. A blocking `subprocess.run` also waits in a selector, but not one
    the loop called, and that is a stall."""
    caller = frame.f_back
    return (
        frame.f_code.co_filename.endswith("selectors.py")
        and caller is not None
        and caller.f_code.co_name == "_run_once"
    )


class StallWatch:
    """Run `heartbeat()` as a task on the loop to watch; `stop()` when done."""

    def __init__(self, path: Path | None = None, *, threshold: float = THRESHOLD, context=None):
        self.path = path or stall_log_path()
        self.threshold = threshold
        # Called from the watcher thread when a stall begins; must only read.
        self.context = context
        self._due: float | None = None
        # The latest late beat, (due, how late it ran); beats that late are at
        # least `threshold` apart, so the watcher sees every one.
        self._late: tuple[float, float] | None = None
        self._loop_thread: int | None = None
        self._gc_started = 0.0
        self._gc_seconds = 0.0
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None

    async def heartbeat(self) -> None:
        self._loop_thread = threading.get_ident()
        if self._watcher is None:
            gc.callbacks.append(self._gc)
            self._watcher = threading.Thread(target=self._watch, name="stall-watch", daemon=True)
            self._watcher.start()
        try:
            while True:
                now = time.monotonic()
                if self._due is not None and now - self._due >= self.threshold:
                    self._late = (self._due, now - self._due)
                self._due = now + BEAT
                await asyncio.sleep(BEAT)
        finally:
            # Shutdown work after cancellation is not a stall.
            self._due = None

    def stop(self) -> None:
        self._stop.set()
        if self._watcher is not None:
            self._watcher.join(timeout=1)
        if self._gc in gc.callbacks:
            gc.callbacks.remove(self._gc)

    def _gc(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._gc_started = time.monotonic()
        else:
            self._gc_seconds += time.monotonic() - self._gc_started

    def _watch(self) -> None:
        stalled = None  # The overdue `_due` being sampled.
        samples: Counter[tuple[str, ...]] = Counter()
        context: dict = {}
        settled = None  # The late beat last accounted for.
        # (wall, process CPU, GC seconds) at the last poll before the stall.
        mark = (time.monotonic(), time.process_time(), self._gc_seconds)
        while not self._stop.wait(POLL):
            now = time.monotonic()
            late = self._late
            if late is not None and late[0] != settled:
                settled = late[0]
                wall, cpu, _ = mark
                busy = time.process_time() - cpu >= BUSY_SHARE * (now - wall)
                if samples or busy:
                    self._write(late[1], samples, context or self._context(), mark)
                stalled, samples, context = None, Counter(), {}
            due = self._due
            overdue = due is not None and now >= due + self.threshold
            if overdue:
                frame = sys._current_frames().get(self._loop_thread)
                if frame is not None and not _idle(frame):
                    if stalled is None:
                        stalled, context = due, self._context()
                    stack = []
                    while frame is not None and len(stack) < FRAMES_KEPT:
                        stack.append(_where(frame))
                        frame = frame.f_back
                    samples[tuple(stack)] += 1
            # Hold the mark while overdue: the first poll after a GIL-held stall
            # can run before the heartbeat does, with the loop already idle.
            if stalled is None and not overdue:
                mark = (now, time.process_time(), self._gc_seconds)
        if samples:
            # Stopped mid-stall: the loop is late by at least this much.
            self._write(time.monotonic() - stalled, samples, context, mark)

    def _context(self) -> dict:
        if self.context is None:
            return {}
        try:
            return dict(self.context())
        except Exception:
            return {}

    def _write(self, late: float, samples: Counter, context: dict, mark: tuple) -> None:
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - late)),
            "pid": os.getpid(),
            "stall_ms": round(late * 1000),
            "gc_ms": round((self._gc_seconds - mark[2]) * 1000, 1),
            "samples": sum(samples.values()),
            **context,
            # Innermost frame first: the top line is what was running. Empty
            # when the stall held the GIL throughout.
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
