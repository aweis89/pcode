import json
import os
import pstats
import subprocess
import sys
import threading
import time
import tracemalloc
from unittest.mock import MagicMock, patch

import pytest

from pcode.app import main
from pcode.profiling import (
    ActivityTotals,
    ResourceProfile,
    activity,
    captures_root,
    new_capture,
    profile_session,
    prune_captures,
    report,
)


def load_samples(directory):
    return [json.loads(line) for line in (directory / "resources.jsonl").read_text().splitlines()]


def cpu_work():
    return sum(i * i for i in range(10_000))


def test_profile_records_cpu_resources_and_private_files(tmp_path):
    directory = tmp_path / "capture"
    with profile_session(directory, cpu=True):
        cpu_work()
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["schema_version"] == 3
    assert summary["complete"] is True
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


def test_descendant_scan_is_throttled_while_known_children_stay_sampled(tmp_path, monkeypatch):
    """Walking the tree reads every process on the machine; measuring known ones is cheap."""
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as child:
        try:
            profile = ResourceProfile(tmp_path / "scan", interval=0.01, discovery_interval=60)
            profile.start()
            walks = []
            try:
                children = profile._process.children

                def counted(**kwargs):
                    walks.append(kwargs)
                    return children(**kwargs)

                monkeypatch.setattr(profile._process, "children", counted)
                deadline = time.monotonic() + 5
                while profile._samples < 5 and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                profile.stop()
        finally:
            child.terminate()
            child.wait(timeout=10)
    samples = load_samples(profile.directory)
    assert len(samples) >= 5
    # Only the forced walk that closes the capture: the rest were throttled.
    assert len(walks) == 1
    # Every sample still measures the child discovered by the first walk.
    assert all(any(p["pid"] == child.pid for p in sample["processes"]) for sample in samples)
    summary = json.loads((profile.directory / "summary.json").read_text())
    assert summary["descendant_scan_seconds"] == 60
    assert summary["sampled_peak_children_rss_bytes"] > 0


def test_scan_interval_never_outpaces_sampling(tmp_path):
    assert ResourceProfile(tmp_path / "slow", interval=10).discovery_interval == 10


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


def test_activity_spans_separate_working_cost_from_idle(tmp_path):
    directory = tmp_path / "activity"
    with profile_session(directory):
        with activity("turn"):
            cpu_work()
        time.sleep(0.05)
    summary = json.loads((directory / "summary.json").read_text())
    turn = summary["activity"]["turn"]
    assert turn["spans"] == 1
    assert turn["cpu_seconds"] > 0
    assert turn["wall_seconds"] <= summary["elapsed_seconds"]
    # The sleep is outside every span, so it lands in the unattributed remainder.
    assert summary["idle_seconds"] >= 0.04
    assert summary["idle_cpu_seconds"] >= 0


def test_activity_is_inert_and_restored_without_a_capture(tmp_path):
    from pcode import profiling

    assert profiling._ACTIVITY is None
    with activity("turn"):
        cpu_work()
    with profile_session(tmp_path / "capture"):
        assert profiling._ACTIVITY is not None
    assert profiling._ACTIVITY is None


def test_open_span_is_attributed_and_named_in_samples(tmp_path):
    profile = ResourceProfile(tmp_path / "open-span", interval=60)
    profile.start()
    assert load_samples(profile.directory)[0]["activity"] is None
    with activity("turn"):
        cpu_work()
        profile._sample()
        assert load_samples(profile.directory)[-1]["activity"] == "turn"
        # A capture stopped mid-turn still keeps the part of that turn it saw.
        profile.stop()
    summary = json.loads((profile.directory / "summary.json").read_text())
    assert summary["activity"]["turn"]["cpu_seconds"] > 0


def test_nested_and_concurrent_spans_count_once(tmp_path):
    totals = ActivityTotals()
    assert totals.label is None
    totals.enter("turn")
    totals.enter("tool")
    totals.enter("turn")
    assert totals.label == "turn"
    totals.exit("turn")
    # Still open at depth one, so its time keeps accruing.
    open_span = totals.snapshot()["turn"]["wall_seconds"]
    time.sleep(0.01)
    assert totals.snapshot()["turn"]["wall_seconds"] > open_span
    totals.exit("turn")
    totals.exit("tool")
    assert totals.label is None
    assert totals.snapshot()["turn"]["wall_seconds"] == totals.snapshot()["turn"]["wall_seconds"]
    assert set(totals.snapshot()) == {"turn", "tool"}
    assert totals.snapshot()["turn"]["spans"] == 2
    # An exit without a matching enter is ignored rather than inventing a span.
    totals.exit("turn")
    assert totals.snapshot()["turn"]["spans"] == 2


def test_live_turn_is_recorded_as_activity(tmp_path):
    import asyncio

    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel

    from pcode.live import AgentRuntime

    async def model(messages, info):
        cpu_work()
        yield "done"

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    directory = tmp_path / "live"

    async def drive():
        return [event async for event in runtime.stream("hello")]

    with profile_session(directory):
        assert asyncio.run(drive())
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["activity"]["turn"]["spans"] == 1
    assert summary["activity"]["turn"]["cpu_seconds"] > 0


def test_summary_is_readable_before_a_capture_is_finalized(tmp_path):
    profile = ResourceProfile(tmp_path / "unfinalized", interval=0.01)
    profile.start()
    try:
        deadline = time.monotonic() + 5
        while profile._samples < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        # What a SIGKILLed session leaves behind: everything but the final pass.
        summary = json.loads((profile.directory / "summary.json").read_text())
        assert summary["complete"] is False
        assert summary["samples"] >= 1
        assert summary["process_cpu_seconds"] > 0
    finally:
        profile.stop()
    assert json.loads((profile.directory / "summary.json").read_text())["complete"] is True
    assert not list(profile.directory.glob(".*partial"))


def test_capture_directory_defaults_to_the_state_directory(tmp_path, monkeypatch):
    assert captures_root() == tmp_path / "state" / "pcode" / "profiles"
    monkeypatch.setenv("PCODE_PROFILE_DIR", str(tmp_path / "elsewhere"))
    assert captures_root() == tmp_path / "elsewhere"
    assert new_capture().parent == tmp_path / "elsewhere"
    assert new_capture() != tmp_path / "elsewhere"


def test_pruning_keeps_recent_automatic_captures_only(tmp_path):
    root = tmp_path / "profiles"
    automatic = [root / f"2026010{index}-120000-99" for index in range(1, 5)]
    named = root / "my-investigation"
    for path in [*automatic, named]:
        path.mkdir(parents=True)
    assert prune_captures(root, keep=2) == automatic[:2]
    assert sorted(path.name for path in root.iterdir()) == [
        automatic[2].name,
        automatic[3].name,
        named.name,
    ]
    assert prune_captures(tmp_path / "missing") == []


def test_report_digests_a_capture(tmp_path):
    directory = tmp_path / "digest"
    with profile_session(directory, cpu=True):
        with activity("turn"):
            cpu_work()
    text = report(directory)
    assert "Ended: clean shutdown" in text
    assert "Activity turn:" in text
    assert "Unattributed CPU:" in text
    assert "Peak RSS:" in text
    assert str(directory / "cpu.txt") in text


def test_cli_bare_profile_writes_to_the_state_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["pcode", "--demo", "--profile"])
    with patch("pcode.app.PreviewApp"):
        main()
    captures = list(captures_root().iterdir())
    assert len(captures) == 1
    assert json.loads((captures[0] / "summary.json").read_text())["complete"] is True


def test_cli_profile_preference_captures_without_flags(monkeypatch, tmp_path):
    from pcode.preferences import save_preferences

    save_preferences(profile="resources")
    monkeypatch.setattr(sys, "argv", ["pcode", "--demo"])
    with patch("pcode.app.PreviewApp"):
        main()
    assert len(list(captures_root().iterdir())) == 1
    # The same default must not keep capturing when this run opts out.
    monkeypatch.setattr(sys, "argv", ["pcode", "--demo", "--no-profile"])
    with patch("pcode.app.PreviewApp"), patch.object(ResourceProfile, "start") as start:
        main()
    start.assert_not_called()
    named = ["pcode", "--demo", "--profile", str(tmp_path / "named"), "--no-profile"]
    monkeypatch.setattr(sys, "argv", named)
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def test_cli_prunes_old_automatic_captures(monkeypatch):
    from pcode import profiling

    stale = captures_root() / "20260101-120000-99"
    stale.mkdir(parents=True)
    monkeypatch.setattr(profiling, "CAPTURES_KEPT", 1)
    monkeypatch.setattr(sys, "argv", ["pcode", "--demo", "--profile"])
    with patch("pcode.app.PreviewApp"):
        main()
    assert not stale.exists()
    assert len(list(captures_root().iterdir())) == 1
