import os
import subprocess
import sys
import time

import leaked_processes
import psutil
import pytest
from leaked_processes import RUN_ENV


def detached(tag: str) -> psutil.Process:
    """A process that left pytest's tree the way leaks do, tagged as run `tag`."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={**os.environ, RUN_ENV: tag},
        start_new_session=True,
    )
    process = psutil.Process(child.pid)
    # Until the child execs, its environment is still pytest's.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if process.environ().get(RUN_ENV) == tag:
                return process
        except psutil.Error:
            pass
        time.sleep(0.01)
    process.kill()
    raise AssertionError(f"{process.pid} never showed {RUN_ENV}={tag}")


@pytest.fixture
def cleanup():
    started = []
    yield started
    for process in started:
        if process.is_running():
            process.kill()


def test_a_run_stops_what_it_leaked_and_nothing_else(cleanup):
    mine = detached("123-1.000")
    live = leaked_processes.run_id(psutil.Process())
    other = detached(live)
    cleanup += [mine, other]

    stopped = leaked_processes.reap_run("123-1.000")

    assert [line.split()[0] for line in stopped] == [str(mine.pid)]
    assert not mine.is_running() or mine.status() == psutil.STATUS_ZOMBIE
    assert other.is_running()


def test_processes_of_a_run_that_died_are_stopped(cleanup):
    orphan = detached("999999-1.000")  # No such controller.
    live = detached(leaked_processes.run_id(psutil.Process()))
    cleanup += [orphan, live]

    leaked_processes.reap_dead_runs()

    assert not orphan.is_running() or orphan.status() == psutil.STATUS_ZOMBIE
    assert live.is_running()


def test_a_nested_run_takes_its_own_tag(monkeypatch):
    monkeypatch.setenv(RUN_ENV, "outer")
    assert leaked_processes.tag_run() != "outer"
