import os
import subprocess
import sys
import time

import leaked_processes
import psutil
import pytest
from leaked_processes import RUN_ENV


@pytest.fixture
def spawn():
    """Start processes that left pytest's tree the way leaks do; reap them after."""
    children = []

    def start(tag: str | None = None) -> psutil.Process:
        env = {**os.environ, RUN_ENV: tag} if tag else None
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=env,
            start_new_session=True,
        )
        children.append(child)
        process = psutil.Process(child.pid)
        # Until the child execs, its environment is still pytest's.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if tag is None or process.environ().get(RUN_ENV) == tag:
                    return process
            except psutil.Error:
                pass
            time.sleep(0.01)
        raise AssertionError(f"{child.pid} never showed {RUN_ENV}={tag}")

    yield start
    for child in children:
        child.kill()
        child.wait()


def gone(process: psutil.Process) -> bool:
    try:
        return process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_a_run_stops_what_it_leaked_and_nothing_else(spawn):
    # Tagged by a run that is alive, so a concurrent reap of dead runs keeps it.
    run = leaked_processes.run_id(spawn())
    mine = spawn(run)
    other = spawn(leaked_processes.run_id(psutil.Process()))

    stopped = leaked_processes.reap_run(run)

    assert [line.split()[0] for line in stopped] == [str(mine.pid)]
    assert gone(mine)
    assert not gone(other)


def test_processes_of_a_run_that_died_are_stopped(spawn):
    orphan = spawn("999999-1.000")  # No such controller.
    live = spawn(leaked_processes.run_id(psutil.Process()))

    stopped = leaked_processes.reap_dead_runs([orphan, live])

    assert [line.split()[0] for line in stopped] == [str(orphan.pid)]
    assert gone(orphan)
    assert not gone(live)


def test_a_run_that_cannot_be_checked_counts_as_alive(monkeypatch):
    def refused(pid):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(leaked_processes.psutil, "Process", refused)
    assert leaked_processes._alive("123-1.000")
    assert leaked_processes._alive("not-a-tag")


def test_a_nested_run_takes_its_own_tag(monkeypatch):
    monkeypatch.setenv(RUN_ENV, "outer")
    assert leaked_processes.tag_run() != "outer"
