import asyncio
import gc
import json
import subprocess
import sys
import time

from pcode.stall_log import StallWatch


def block_the_loop(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass


async def watched(watch, body, *, settle=0.15):
    task = asyncio.create_task(watch.heartbeat())
    try:
        await asyncio.sleep(0.1)
        await body()
        await asyncio.sleep(settle)
    finally:
        task.cancel()
        watch.stop()


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_blocked_loop_logs_one_stall_naming_the_blocker(tmp_path):
    path = tmp_path / "stalls.jsonl"
    watch = StallWatch(path, context=lambda: {"busy": True})

    async def body():
        block_the_loop(0.4)

    asyncio.run(watched(watch, body))
    [record] = records(path)
    assert 250 <= record["stall_ms"] < 1000
    assert record["busy"] is True
    top = record["stacks"][0]["frames"][0]
    assert "block_the_loop" in top and "test_stall_log.py" in top


def test_a_blocking_subprocess_is_a_stall_though_it_waits_in_a_selector(tmp_path):
    path = tmp_path / "stalls.jsonl"

    async def body():
        subprocess.run([sys.executable, "-c", "import time; time.sleep(0.4)"], capture_output=True)

    asyncio.run(watched(StallWatch(path), body))
    [record] = records(path)
    assert any("subprocess" in frame for frame in record["stacks"][0]["frames"])


def test_a_stall_holding_the_gil_is_logged_without_stacks(tmp_path):
    path = tmp_path / "stalls.jsonl"
    garbage = []
    for _ in range(300_000):
        node = []
        node.append(node)
        garbage.append(node)

    async def body():
        # The watcher thread cannot take the GIL until this block ends.
        interval = sys.getswitchinterval()
        sys.setswitchinterval(30)
        try:
            garbage.clear()
            gc.collect()
            block_the_loop(0.3)
        finally:
            sys.setswitchinterval(interval)

    asyncio.run(watched(StallWatch(path), body))
    [record] = records(path)
    assert record["stacks"] == [] and record["stall_ms"] >= 200
    assert record["gc_ms"] > 0


def test_a_stall_still_open_at_stop_is_written(tmp_path):
    path = tmp_path / "stalls.jsonl"

    async def body():
        block_the_loop(0.3)

    # Stop the moment the block ends, before the heartbeat can run late.
    asyncio.run(watched(StallWatch(path), body, settle=0))
    [record] = records(path)
    assert record["stall_ms"] >= 100
    assert "block_the_loop" in record["stacks"][0]["frames"][0]


def test_an_idle_loop_logs_nothing(tmp_path):
    path = tmp_path / "stalls.jsonl"

    async def body():
        await asyncio.sleep(0.4)

    # A generous threshold: a loaded test machine can starve the process briefly.
    asyncio.run(watched(StallWatch(path, threshold=0.3), body))
    assert records(path) == []


def test_a_short_block_under_the_threshold_is_ignored(tmp_path):
    path = tmp_path / "stalls.jsonl"

    async def body():
        block_the_loop(0.03)

    asyncio.run(watched(StallWatch(path, threshold=0.3), body))
    assert records(path) == []
