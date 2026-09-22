"""Opt-in, local-only profiling. Never capture arguments, locals, or source text."""

from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import sys
import threading
import time
import tracemalloc
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

PROFILE_MODES = ("off", "resources", "cpu", "memory")
"""The `profile` preference: no capture, sampling only, or sampling plus one tracer."""

CAPTURES_KEPT = 20
"""Automatically named captures retained. An always-on capture writes one directory
per session, so unbounded growth would be the cost of leaving the setting on."""

_AUTOMATIC_NAME = re.compile(r"\d{8}-\d{6}-\d+\Z")


def captures_root() -> Path:
    """`PCODE_PROFILE_DIR` overrides where automatically named captures are written."""
    override = os.environ.get("PCODE_PROFILE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "profiles"


def new_capture(root: Path | None = None) -> Path:
    """A fresh per-session directory, since a capture refuses to reuse an existing one."""
    root = captures_root() if root is None else root
    return root / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"


def prune_captures(root: Path | None = None, keep: int | None = None) -> list[Path]:
    """Delete all but the newest `keep` automatic captures, by name.

    Only `new_capture` names are considered, so a capture written to a directory
    the user named is never removed. Retention is best effort: a capture that
    cannot be deleted is left in place rather than failing the session.
    """
    root = captures_root() if root is None else root
    keep = CAPTURES_KEPT if keep is None else keep
    try:
        candidates = sorted(
            path for path in root.iterdir() if path.is_dir() and _AUTOMATIC_NAME.match(path.name)
        )
    except OSError:
        return []
    removed = []
    for path in candidates[: max(0, len(candidates) - keep)]:
        try:
            shutil.rmtree(path)
        except OSError:
            continue
        removed.append(path)
    return removed


class ActivityTotals:
    """Wall and process CPU time spent inside named spans; the rest of a run is idle.

    Spans are reference counted per label rather than stacked: concurrent work
    opens one span, and the earliest still-open label names the current activity.
    Overlapping labels each see the whole process's CPU for their own span, so
    their CPU seconds can sum to more than the process spent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[str, tuple[int, float, float]] = {}
        self._totals: dict[str, dict[str, float]] = {}

    @property
    def label(self) -> str | None:
        """The longest-running open span's label, or None while nothing is running."""
        return next(iter(self._open), None)

    def enter(self, label: str) -> None:
        with self._lock:
            depth, wall, cpu = self._open.get(label, (0, 0.0, 0.0))
            if depth == 0:
                wall, cpu = time.monotonic(), time.process_time()
            self._open[label] = (depth + 1, wall, cpu)
            self._totals.setdefault(label, {"wall_seconds": 0.0, "cpu_seconds": 0.0, "spans": 0})
            self._totals[label]["spans"] += 1

    def exit(self, label: str) -> None:
        with self._lock:
            current = self._open.get(label)
            if current is None:
                return
            depth, wall, cpu = current
            if depth > 1:
                # Assigning an existing key keeps insertion order, so a nested
                # span does not make its label look like the newest one.
                self._open[label] = (depth - 1, wall, cpu)
                return
            del self._open[label]
            entry = self._totals[label]
            entry["wall_seconds"] += time.monotonic() - wall
            entry["cpu_seconds"] += time.process_time() - cpu

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Totals including the elapsed part of spans that are still open."""
        with self._lock:
            now, cpu_now = time.monotonic(), time.process_time()
            totals = {label: dict(entry) for label, entry in self._totals.items()}
            for label, (_, wall, cpu) in self._open.items():
                totals[label]["wall_seconds"] += now - wall
                totals[label]["cpu_seconds"] += cpu_now - cpu
            return totals


_ACTIVITY: ActivityTotals | None = None
"""Set only while a capture runs, so `activity()` costs an attribute read otherwise."""


@contextmanager
def activity(label: str) -> Iterator[None]:
    """Attribute the enclosed work to `label` when a capture is running.

    Safe to call from anywhere: without a capture it does nothing, and a capture
    that stops mid-span still receives that span's time.
    """
    totals = _ACTIVITY
    if totals is None:
        yield
        return
    totals.enter(label)
    try:
        yield
    finally:
        totals.exit(label)


class ResourceProfile:
    """Optional function timings plus sampled process-tree resource usage.

    Samples are streamed to disk, not retained in memory. Descendants can disappear
    between samples; their totals are observations, not complete job accounting.
    """

    def __init__(
        self, directory: Path, *, cpu: bool = False, memory: bool = False, interval: float = 1.0
    ):
        self.directory = directory
        self.memory = memory
        self.cpu = cpu
        self.interval = interval
        self._stop = threading.Event()
        self._previous: dict[tuple[int, float], tuple[float, float]] = {}
        self._peak_rss = 0
        self._peak_children_rss = 0
        self._samples = 0
        self._sampling_errors = 0
        self.activity = ActivityTotals()
        self._cpu = None
        self._cpu_clock = None

    def _open(self, name: str):
        fd = os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return os.fdopen(fd, "w", encoding="utf-8")

    def _replace(self, name: str, payload: dict) -> None:
        """Rewrite a file atomically, so an interrupted capture leaves the last one whole."""
        temporary = self.directory / f".{name}.partial"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(payload, output, indent=2)
                output.write("\n")
            os.replace(temporary, self.directory / name)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def start(self) -> None:
        global _ACTIVITY
        import psutil

        if self.memory and tracemalloc.is_tracing():
            raise ValueError("allocation tracing is already active")
        if self.cpu:
            # Not cProfile: on Python 3.14 it can observe worker threads, and a
            # `time.thread_time` timer then produces negative timings. Yappi
            # accounts CPU per thread.
            import yappi

            if yappi.is_running() or yappi.get_func_stats():
                raise ValueError("function profiler is already in use")
            self._cpu = yappi
            self._cpu_clock = yappi.get_clock_type()
        # Refuse reuse rather than clobbering an earlier capture or following a symlink.
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        self._process = psutil.Process()
        self._stream = self._open("resources.jsonl")
        self._started = time.monotonic()
        self._cpu_started = time.process_time()
        self.activity = ActivityTotals()
        _ACTIVITY = self.activity
        self._thread = threading.Thread(target=self._monitor, name="resource-profile", daemon=True)
        try:
            if self.memory:
                tracemalloc.start(10)
            self._sample()
            self._thread.start()
            if self._cpu is not None:
                self._cpu.set_clock_type("cpu")
                self._cpu.start(builtins=True, profile_threads=True)
        except BaseException:
            _ACTIVITY = None
            self._stop.set()
            if self._thread.is_alive():
                self._thread.join()
            try:
                self._stream.close()
            finally:
                if self.memory:
                    tracemalloc.stop()
                self._clear_cpu()
            raise

    def _sample(self) -> None:
        import psutil

        now = time.monotonic()
        processes = []
        previous = {}
        try:
            descendants = self._process.children(recursive=True)
        except psutil.Error:
            descendants = []
            self._sampling_errors += 1
        for process in [self._process, *descendants]:
            try:
                with process.oneshot():
                    identity = (process.pid, process.create_time())
                    cpu = process.cpu_times()
                    cpu_seconds = cpu.user + cpu.system
                    rss = process.memory_info().rss
                    threads = process.num_threads()
                    ppid = process.ppid()
                before = self._previous.get(identity)
                percent = None
                if before is not None and now > before[0]:
                    percent = max(0.0, 100 * (cpu_seconds - before[1]) / (now - before[0]))
                previous[identity] = (now, cpu_seconds)
                processes.append(
                    dict(
                        pid=process.pid,
                        ppid=ppid,
                        role="pcode" if process.pid == self._process.pid else "child",
                        cpu_seconds=cpu_seconds,
                        cpu_percent=percent,
                        rss_bytes=rss,
                        threads=threads,
                    )
                )
            except psutil.Error:
                self._sampling_errors += 1
        self._previous = previous
        own_rss = next((p["rss_bytes"] for p in processes if p["role"] == "pcode"), 0)
        children_rss = sum(p["rss_bytes"] for p in processes if p["role"] == "child")
        self._peak_rss = max(self._peak_rss, own_rss)
        self._peak_children_rss = max(self._peak_children_rss, children_rss)
        sample = dict(
            elapsed_seconds=now - self._started,
            activity=self.activity.label,
            processes=processes,
        )
        if self.memory:
            current, peak = tracemalloc.get_traced_memory()
            sample.update(python_current_bytes=current, python_peak_bytes=peak)
        self._stream.write(json.dumps(sample) + "\n")
        self._stream.flush()
        self._samples += 1
        # A live session is usually killed with its terminal rather than exited
        # cleanly, so the aggregate is republished with every sample instead of
        # only at shutdown.
        self._replace("summary.json", self._summary(complete=False))

    def _monitor(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._sample()
            except OSError:
                self._sampling_errors += 1
                return

    def _clear_cpu(self) -> None:
        if self._cpu is not None:
            self._cpu.stop()
            self._cpu.clear_stats()
            self._cpu.set_clock_type(self._cpu_clock)

    def _write_cpu(self) -> None:
        if self._cpu is None:
            return
        with self._open("cpu.pstats"):
            pass
        stats = self._cpu.convert2pstats(self._cpu.get_func_stats())
        stats.dump_stats(str(self.directory / "cpu.pstats"))
        report = io.StringIO()
        stats.stream = report
        stats.strip_dirs()
        report.write(
            "Function CPU seconds across Python threads; sleeping/network waits excluded.\n"
        )
        report.write(
            "Includes profiling overhead. Native-only threads/children are not attributed.\n"
        )
        report.write("\nTop cumulative CPU:\n")
        stats.sort_stats("cumulative").print_stats(50)
        report.write("\nTop self CPU:\n")
        stats.sort_stats("tottime").print_stats(50)
        with self._open("cpu.txt") as output:
            output.write(report.getvalue())

    def _summary(
        self, *, complete: bool, elapsed: float | None = None, cpu_seconds: float | None = None
    ) -> dict:
        if elapsed is None:
            elapsed = time.monotonic() - self._started
        if cpu_seconds is None:
            cpu_seconds = time.process_time() - self._cpu_started
        attributed = self.activity.snapshot()
        busy = sum(entry["wall_seconds"] for entry in attributed.values())
        busy_cpu = sum(entry["cpu_seconds"] for entry in attributed.values())
        return dict(
            schema_version=2,
            # A capture that was killed still has every field except this one.
            complete=complete,
            python=platform.python_version(),
            platform=sys.platform,
            elapsed_seconds=elapsed,
            process_cpu_seconds=cpu_seconds,
            average_cpu_percent=100 * cpu_seconds / elapsed if elapsed else 0,
            sampled_peak_rss_bytes=self._peak_rss,
            sampled_peak_children_rss_bytes=self._peak_children_rss,
            samples=self._samples,
            sampling_errors=self._sampling_errors,
            interval_seconds=self.interval,
            memory_tracing=self.memory,
            function_clock="cpu" if self._cpu is not None else None,
            function_scope="python_threads" if self._cpu is not None else None,
            activity=attributed,
            # Time outside every span: what the session cost while doing nothing
            # the app named. Overlapping spans can push these below zero.
            idle_seconds=max(0.0, elapsed - busy),
            idle_cpu_seconds=max(0.0, cpu_seconds - busy_cpu),
        )

    def stop(self) -> None:
        global _ACTIVITY

        if self._cpu is not None:
            self._cpu.stop()
        elapsed = time.monotonic() - self._started
        cpu_seconds = time.process_time() - self._cpu_started
        _ACTIVITY = None
        self._stop.set()
        self._thread.join()
        try:
            self._sample()
            self._stream.close()
            memory_usage = None
            snapshot = None
            if self.memory:
                memory_usage = tracemalloc.get_traced_memory()
                snapshot = tracemalloc.take_snapshot()
                tracemalloc.stop()
            self._write_cpu()
            summary = self._summary(complete=True, elapsed=elapsed, cpu_seconds=cpu_seconds)
            if snapshot is not None and memory_usage is not None:
                current, peak = memory_usage
                summary.update(python_current_bytes=current, python_peak_bytes=peak)
                # No source lines or heap contents, only allocation locations/sizes.
                allocations = [
                    dict(
                        filename=stat.traceback[0].filename,
                        line=stat.traceback[0].lineno,
                        size_bytes=stat.size,
                        count=stat.count,
                    )
                    for stat in snapshot.statistics("lineno")[:50]
                ]
                with self._open("allocations.json") as output:
                    json.dump(allocations, output, indent=2)
                    output.write("\n")
            self._replace("summary.json", summary)
        finally:
            try:
                self._stream.close()
            finally:
                if self.memory:
                    tracemalloc.stop()
                self._clear_cpu()


@contextmanager
def profile_session(directory: Path, *, cpu: bool = False, memory: bool = False) -> Iterator[None]:
    profile = ResourceProfile(directory, cpu=cpu, memory=memory)
    profile.start()
    try:
        yield
    finally:
        try:
            profile.stop()
        except Exception as error:
            # Profiling must not hide the application's error or change its exit code.
            print(f"Profile could not be finalized ({type(error).__name__}).", file=sys.stderr)
        else:
            print(f"Profile saved to {directory}", file=sys.stderr)


def _megabytes(value: float) -> str:
    return f"{value / 1_000_000:.1f} MB"


def report(directory: Path) -> str:
    """A readable digest of one capture, so the artifacts need no extra tooling.

    Reads only what the capture wrote. A killed session has no `cpu`/`allocations`
    artifacts and an incomplete summary; both are reported rather than treated as
    an error.
    """
    summary = json.loads((directory / "summary.json").read_text())
    elapsed = summary.get("elapsed_seconds") or 0.0
    lines = [
        f"Capture: {directory}",
        "Ended: clean shutdown" if summary.get("complete") else "Ended: killed (last sample)",
        f"Duration: {elapsed:.1f} s over {summary.get('samples', 0)} samples",
        f"Process CPU: {summary.get('process_cpu_seconds', 0):.1f} s"
        f" ({summary.get('average_cpu_percent', 0):.1f}% of one core)",
        f"Peak RSS: {_megabytes(summary.get('sampled_peak_rss_bytes', 0))}"
        f" (children {_megabytes(summary.get('sampled_peak_children_rss_bytes', 0))})",
    ]

    def share(cpu: float) -> str:
        return f" ({100 * cpu / elapsed:.1f}% of one core)" if elapsed else ""

    idle_cpu = summary.get("idle_cpu_seconds", 0.0)
    lines.append(
        f"Unattributed CPU: {idle_cpu:.1f} s{share(idle_cpu)}"
        f" over {summary.get('idle_seconds', 0):.1f} s"
    )
    for label, entry in sorted(
        summary.get("activity", {}).items(), key=lambda item: -item[1]["cpu_seconds"]
    ):
        lines.append(
            f"Activity {label}: {entry['cpu_seconds']:.1f} s CPU{share(entry['cpu_seconds'])}"
            f" over {entry['wall_seconds']:.1f} s in {int(entry['spans'])} spans"
        )
    children: dict[int, tuple[float, int]] = {}
    own_peak_threads = 0
    try:
        with (directory / "resources.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                sample = json.loads(line)
                for process in sample["processes"]:
                    if process["role"] == "pcode":
                        own_peak_threads = max(own_peak_threads, process["threads"])
                        continue
                    # Per-PID CPU is a lifetime counter, so the last observation
                    # is that child's total, not a per-sample cost.
                    seen = children.get(process["pid"], (0.0, 0))
                    children[process["pid"]] = (
                        max(seen[0], process["cpu_seconds"]),
                        max(seen[1], process["rss_bytes"]),
                    )
    except (OSError, ValueError, KeyError):
        lines.append("Samples: unreadable or truncated")
    else:
        lines.append(f"Peak threads in pcode: {own_peak_threads}")
        ranked = sorted(children.items(), key=lambda item: -item[1][0])[:5]
        for pid, (cpu, rss) in ranked:
            lines.append(f"Child {pid}: {cpu:.1f} s CPU seen, peak RSS {_megabytes(rss)}")
        if not ranked:
            lines.append("Children: none observed")
    if summary.get("sampling_errors"):
        lines.append(f"Incomplete coverage: {summary['sampling_errors']} sampling errors")
    for artifact, description in (
        ("cpu.txt", "function CPU"),
        ("allocations.json", "live allocations"),
    ):
        if (directory / artifact).exists():
            lines.append(f"See {directory / artifact} for {description}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m pcode.profiling",
        description="Summarize a pcode profiling capture, or list recent captures",
    )
    parser.add_argument("directory", type=Path, nargs="?", help="Capture directory; omit to list")
    arguments = parser.parse_args(argv)
    if arguments.directory is None:
        root = captures_root()
        captures = sorted(path for path in root.glob("*") if (path / "summary.json").exists())
        if not captures:
            print(f"No captures under {root}", file=sys.stderr)
            return 1
        for path in captures:
            print(path)
        return 0
    try:
        print(report(arguments.directory))
    except (OSError, ValueError) as error:
        print(f"Cannot read capture ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
