import json
import os
import pstats
import subprocess
import sys
import threading
import tracemalloc
from unittest.mock import MagicMock, patch

import pytest

from pcode.app import main
from pcode.profiling import ResourceProfile, profile_session


def load_samples(directory):
    return [json.loads(line) for line in (directory / "resources.jsonl").read_text().splitlines()]


def cpu_work():
    return sum(i * i for i in range(10_000))


def test_profile_records_cpu_resources_and_private_files(tmp_path):
    directory = tmp_path / "capture"
    with profile_session(directory, cpu=True):
        cpu_work()
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["schema_version"] == 1
    assert summary["elapsed_seconds"] > 0
    assert summary["process_cpu_seconds"] > 0
    assert summary["sampled_peak_rss_bytes"] > 0
    assert summary["samples"] >= 2
    assert summary["sampling_errors"] == 0
    assert summary["function_clock"] == "cpu"
    stats = pstats.Stats(str(directory / "cpu.pstats"))
    assert any(key[2] == "cpu_work" for key in stats.stats)
    assert "Top self CPU" in (directory / "cpu.txt").read_text()
    samples = load_samples(directory)
    own = next(p for p in samples[-1]["processes"] if p["role"] == "pcode")
    assert own["pid"] == os.getpid()
    assert own["cpu_percent"] is not None
    assert own["threads"] >= 1
    assert not (directory / "allocations.json").exists()
    if os.name != "nt":
        assert directory.stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())


def test_memory_trace_has_locations_not_contents_and_is_stopped(tmp_path):
    directory = tmp_path / "memory"
    sentinel = "PRIVATE_PAYLOAD_DO_NOT_RECORD"
    with profile_session(directory, memory=True):
        payload = [bytearray(1000) for _ in range(100)]
        cpu_work()
    assert len(payload) == 100
    assert not tracemalloc.is_tracing()
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["python_peak_bytes"] >= 100_000
    assert "python_current_bytes" in load_samples(directory)[-1]
    allocations = json.loads((directory / "allocations.json").read_text())
    assert any(item["filename"] == __file__ for item in allocations)
    assert all(set(item) == {"filename", "line", "size_bytes", "count"} for item in allocations)
    assert all(sentinel.encode() not in path.read_bytes() for path in directory.iterdir())


def test_profile_tracks_child_without_arguments_or_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PROFILE_SECRET", "not-for-the-report")
    directory = tmp_path / "child"
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "private-command-argument"]
    ) as child:
        try:
            with profile_session(directory):
                cpu_work()
        finally:
            child.terminate()
            child.wait(timeout=10)
    processes = load_samples(directory)[0]["processes"]
    assert any(p["pid"] == child.pid and p["role"] == "child" for p in processes)
    for path in directory.iterdir():
        data = path.read_bytes()
        assert b"private-command-argument" not in data
        assert b"not-for-the-report" not in data


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_exception_still_flushes_profile_and_stops_thread(tmp_path, exception):
    directory = tmp_path / "failure"
    with pytest.raises(exception):
        with profile_session(directory):
            raise exception()
    assert (directory / "summary.json").exists()
    assert not any(t.name == "resource-profile" for t in threading.enumerate())


def test_existing_directory_is_not_modified(tmp_path):
    marker = tmp_path / "summary.json"
    marker.write_text("keep me")
    with pytest.raises(FileExistsError):
        with profile_session(tmp_path):
            pytest.fail("must not run")
    assert marker.read_text() == "keep me"


def test_existing_memory_tracer_is_not_stopped(tmp_path):
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="already active"):
            with profile_session(tmp_path / "capture", memory=True):
                pytest.fail("must not run")
        assert tracemalloc.is_tracing()
    finally:
        tracemalloc.stop()


def test_finalization_failure_does_not_mask_exception(tmp_path, monkeypatch, capsys):
    with pytest.raises(RuntimeError, match="original"):
        with profile_session(tmp_path / "capture", memory=True):

            def fail_open(*args):
                raise OSError("private detail")

            monkeypatch.setattr(ResourceProfile, "_open", fail_open)
            raise RuntimeError("original")
    assert "could not be finalized (OSError)" in capsys.readouterr().err
    assert not tracemalloc.is_tracing()
    assert not any(t.name == "resource-profile" for t in threading.enumerate())


def test_cli_profile_and_memory_validation(monkeypatch, tmp_path):
    directory = tmp_path / "cli"
    monkeypatch.setattr(sys, "argv", ["pcode", "--demo", "--profile", str(directory)])
    with patch("pcode.app.PreviewApp") as app:
        main()
    app.return_value.transcript.events.assert_called_once()
    assert (directory / "summary.json").exists()
    monkeypatch.setattr(sys, "argv", ["pcode", "--demo", "--profile-memory"])
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def test_cli_disabled_does_not_start_profiler(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pcode", "--demo"])
    with patch("pcode.app.PreviewApp"), patch.object(ResourceProfile, "start") as start:
        main()
    start.assert_not_called()


def test_periodic_sampling_and_shutdown(tmp_path):
    profile = ResourceProfile(tmp_path / "periodic", interval=0.01)
    profile.start()
    try:
        # Observe the monitor's actual write rather than assuming a scheduling delay.
        from time import monotonic, sleep

        deadline = monotonic() + 5
        while profile._samples < 3 and monotonic() < deadline:
            sleep(0.01)
        assert profile._samples >= 3
    finally:
        profile.stop()
    assert not profile._thread.is_alive()
    assert len(load_samples(profile.directory)) >= 4


def test_resource_only_capture_does_not_enable_call_or_allocation_tracing(tmp_path):
    with patch("yappi.start") as cpu:
        with profile_session(tmp_path / "resources"):
            assert not tracemalloc.is_tracing()
    cpu.assert_not_called()
    assert not (tmp_path / "resources" / "cpu.pstats").exists()


@pytest.mark.parametrize("during_start", [False, True])
def test_close_failure_always_stops_allocation_tracing(tmp_path, monkeypatch, during_start):
    profile = ResourceProfile(tmp_path / "close-failure", memory=True)

    class FailingStream:
        def close(self):
            raise OSError("close failure")

    if during_start:
        monkeypatch.setattr(profile, "_open", lambda name: FailingStream())
        monkeypatch.setattr(profile, "_sample", lambda: None)

        def fail_start():
            raise RuntimeError("thread failure")

        monkeypatch.setattr(threading.Thread, "start", lambda self: fail_start())
        with pytest.raises(OSError):
            profile.start()
    else:
        profile.start()
        stream = profile._stream
        monkeypatch.setattr(profile, "_sample", lambda: None)
        profile._stream = FailingStream()
        try:
            with pytest.raises(OSError):
                profile.stop()
        finally:
            stream.close()
    assert not tracemalloc.is_tracing()
    assert not profile._thread.is_alive()


def test_disappearing_child_does_not_abort_capture(tmp_path, monkeypatch):
    import psutil

    child = MagicMock(pid=999999)
    child.oneshot.side_effect = psutil.NoSuchProcess(child.pid)
    profile = ResourceProfile(tmp_path / "disappearing")
    profile.start()
    try:
        monkeypatch.setattr(profile._process, "children", lambda **kwargs: [child])
    finally:
        profile.stop()
    summary = json.loads((profile.directory / "summary.json").read_text())
    assert summary["sampling_errors"] == 1
    assert summary["sampled_peak_rss_bytes"] > 0


def test_function_clock_remains_valid_with_background_sampling(tmp_path):
    from time import monotonic

    profile = ResourceProfile(tmp_path / "clock", cpu=True, interval=0.01)
    profile.start()
    try:
        deadline = monotonic() + 0.1
        while monotonic() < deadline:
            cpu_work()
    finally:
        profile.stop()
    stats = pstats.Stats(str(profile.directory / "cpu.pstats"))
    assert all(value[2] >= 0 and value[3] >= 0 for value in stats.stats.values())
    assert len(load_samples(profile.directory)) > 2


def test_benchmark_honors_wide_terminal_and_discards_output(monkeypatch):
    import asyncio

    from rich.console import Console

    from pcode import profile_benchmark

    widths = []

    def console(**kwargs):
        result = Console(**kwargs)
        widths.append(result.width)
        return result

    monkeypatch.setattr(profile_benchmark, "Console", console)
    for kind in ("prose", "list", "fence"):
        assert asyncio.run(profile_benchmark.stream_workload(kind, 3, 160)) > 0
    assert widths == [160] * 3
    sink = profile_benchmark.CountingOutput()
    assert sink.write("abc") == 3
    assert sink.characters == 3
    assert not hasattr(sink, "getvalue")


def test_cpu_profile_covers_workers_but_excludes_sleep_and_restores_clock(tmp_path):
    import time

    import yappi

    def sleeping_worker():
        time.sleep(0.1)
        cpu_work()

    old_clock = yappi.get_clock_type()
    yappi.set_clock_type("wall")
    directory = tmp_path / "threads"
    try:
        with profile_session(directory, cpu=True):
            worker = threading.Thread(target=sleeping_worker)
            worker.start()
            worker.join()
        assert not yappi.is_running()
        assert not yappi.get_func_stats()
        assert yappi.get_clock_type() == "wall"
    finally:
        yappi.set_clock_type(old_clock)
    stats = pstats.Stats(str(directory / "cpu.pstats"))
    times = next(value for key, value in stats.stats.items() if key[2] == "sleeping_worker")
    assert 0 < times[3] < 0.08  # CPU work is present; the 100ms sleep is not.


def test_existing_cpu_profiler_is_not_disturbed(tmp_path):
    import yappi

    yappi.start()
    try:
        with pytest.raises(ValueError, match="already in use"):
            with profile_session(tmp_path / "capture", cpu=True):
                pytest.fail("must not run")
        assert yappi.is_running()
    finally:
        yappi.stop()
        yappi.clear_stats()
