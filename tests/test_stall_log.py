import asyncio
import json
import time

from pcode.stall_log import StallWatch


def block_the_loop(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass


async def watched(watch, body):
    task = asyncio.create_task(watch.heartbeat())
    try:
        await asyncio.sleep(0.1)
        await body()
        # Long enough for the watcher to see the heartbeat resume.
        await asyncio.sleep(0.15)
    finally:
        task.cancel()
        watch.stop()


def test_a_blocked_loop_logs_one_stall_naming_the_blocker(tmp_path):
    path = tmp_path / "stalls.jsonl"
    watch = StallWatch(path, context=lambda: {"busy": True})

    async def body():
        block_the_loop(0.4)

    asyncio.run(watched(watch, body))
    [record] = [json.loads(line) for line in path.read_text().splitlines()]
    assert 250 <= record["stall_ms"] < 1000
    assert record["busy"] is True
    top = record["stacks"][0]["frames"][0]
    assert "block_the_loop" in top and "test_stall_log.py" in top


def test_an_idle_loop_logs_nothing(tmp_path):
    path = tmp_path / "stalls.jsonl"

    async def body():
        await asyncio.sleep(0.4)

    asyncio.run(watched(StallWatch(path), body))
    assert not path.exists()


def test_a_short_block_under_the_threshold_is_ignored(tmp_path):
    path = tmp_path / "stalls.jsonl"

    async def body():
        block_the_loop(0.03)

    asyncio.run(watched(StallWatch(path), body))
    assert not path.exists()
