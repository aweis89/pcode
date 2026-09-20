"""Opt-in, local-only profiling. Never capture arguments, locals, or source text."""

from __future__ import annotations

import io
import json
import os
import platform
import sys
import threading
import time
import tracemalloc
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


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
        self._cpu = None
        self._cpu_clock = None

    def _open(self, name: str):
        fd = os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return os.fdopen(fd, "w", encoding="utf-8")

    def start(self) -> None:
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
        sample = dict(elapsed_seconds=now - self._started, processes=processes)
        if self.memory:
            current, peak = tracemalloc.get_traced_memory()
            sample.update(python_current_bytes=current, python_peak_bytes=peak)
        self._stream.write(json.dumps(sample) + "\n")
        self._stream.flush()
        self._samples += 1

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

    def stop(self) -> None:
        if self._cpu is not None:
            self._cpu.stop()
        elapsed = time.monotonic() - self._started
        cpu_seconds = time.process_time() - self._cpu_started
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
            summary = dict(
                schema_version=1,
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
            )
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
            with self._open("summary.json") as output:
                json.dump(summary, output, indent=2)
                output.write("\n")
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
