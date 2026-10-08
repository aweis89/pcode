import asyncio
import gc
import json
import os
import signal
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


def test_a_slow_log_write_is_not_mistaken_for_a_suspension(tmp_path):
    """Writing one stall can take a while on a slow disk; the next stall, already
    under way, must still be logged."""
    path = tmp_path / "stalls.jsonl"
    watch = StallWatch(path)
    write = watch._write

    def slow_write(*args, **kwargs):
        time.sleep(0.15)
        write(*args, **kwargs)

    watch._write = slow_write

    async def body():
        time.sleep(0.3)
        await asyncio.sleep(0.01)
        time.sleep(0.6)

    asyncio.run(watched(watch, body, settle=0.4))
    assert len(records(path)) == 2


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


def test_a_suspended_process_logs_no_stall(tmp_path):
    """Ctrl+Z stops the watcher with the loop: a late beat with no CPU behind it is not a stall."""
    path = tmp_path / "stalls.jsonl"
    script = f"""
import asyncio, sys
from pathlib import Path
from pcode.stall_log import StallWatch

async def main():
    watch = StallWatch(Path({str(path)!r}))
    task = asyncio.create_task(watch.heartbeat())
    resumed = asyncio.Event()
    asyncio.get_running_loop().add_reader(sys.stdin, resumed.set)
    await asyncio.sleep(0.1)
    print("ready", flush=True)
    # Idle in the loop's own selector, as a session waiting for keys is.
    await resumed.wait()
    await asyncio.sleep(0.3)
    task.cancel()
    watch.stop()

asyncio.run(main())
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline() == "ready\n"
        child.send_signal(signal.SIGSTOP)
        time.sleep(0.6)
        child.send_signal(signal.SIGCONT)
        child.stdin.write("\n")
        child.stdin.close()
        assert child.wait(timeout=30) == 0
    finally:
        child.kill()
        child.wait()
        child.stdout.close()
    assert records(path) == []


def test_a_process_suspended_mid_callback_logs_no_stall(tmp_path):
    """Ctrl+Z can land while a callback runs, not only while the loop idles: the
    loop is then busy when both threads resume, which is no stall either."""
    path = tmp_path / "stalls.jsonl"
    script = f"""
import asyncio, os, signal, time
from pathlib import Path
from pcode.stall_log import StallWatch

async def main():
    watch = StallWatch(Path({str(path)!r}))
    task = asyncio.create_task(watch.heartbeat())
    await asyncio.sleep(0.1)
    os.kill(os.getpid(), signal.SIGSTOP)
    # Resumed mid-callback: work on, briefly enough not to be a stall itself.
    end = time.monotonic() + 0.06
    while time.monotonic() < end:
        pass
    await asyncio.sleep(0.3)
    task.cancel()
    watch.stop()

asyncio.run(main())
"""
    child = subprocess.Popen([sys.executable, "-c", script])
    try:
        _, status = os.waitpid(child.pid, os.WUNTRACED)
        assert os.WIFSTOPPED(status)
        time.sleep(0.6)
        child.send_signal(signal.SIGCONT)
        assert child.wait(timeout=30) == 0
    finally:
        child.kill()
        child.wait()
    assert records(path) == []


def test_a_stall_on_both_sides_of_a_suspension_is_logged_without_it(tmp_path):
    """Ctrl+Z pressed because the terminal froze, then `fg`: the freeze is still
    there, before and after, but the time spent stopped is not part of it."""
    path = tmp_path / "stalls.jsonl"
    script = f"""
import asyncio, os, signal, time
from pathlib import Path
from pcode.stall_log import StallWatch

def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass

async def main():
    watch = StallWatch(Path({str(path)!r}))
    task = asyncio.create_task(watch.heartbeat())
    await asyncio.sleep(0.1)
    spin(0.3)
    os.kill(os.getpid(), signal.SIGSTOP)
    spin(1.0)
    await asyncio.sleep(0.3)
    task.cancel()
    watch.stop()

asyncio.run(main())
"""
    child = subprocess.Popen([sys.executable, "-c", script])
    try:
        _, status = os.waitpid(child.pid, os.WUNTRACED)
        assert os.WIFSTOPPED(status)
        time.sleep(0.6)
        child.send_signal(signal.SIGCONT)
        assert child.wait(timeout=30) == 0
    finally:
        child.kill()
        child.wait()
    stalls = sorted(record["stall_ms"] for record in records(path))
    # Before the stop, then after it: neither spans the 600 ms spent stopped.
    assert len(stalls) == 2, stalls
    assert 150 <= stalls[0] < 600 and 800 <= stalls[1] < 1500, stalls


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
