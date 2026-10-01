import os
import subprocess
import sys
import time
import uuid

import leaked_processes
import psutil
import pytest
from leaked_processes import RUN_ENV, RUNS_DIR


@pytest.fixture
def spawn():
    """Start processes that left pytest's tree the way leaks do; reap them after."""
    children = []

    def start(tag: str) -> psutil.Process:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env={**os.environ, RUN_ENV: tag},
            start_new_session=True,
        )
        children.append(child)
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
        raise AssertionError(f"{child.pid} never showed {RUN_ENV}={tag}")

    yield start
    for child in children:
        child.kill()
        child.wait()


@pytest.fixture
def run():
    """A live run of this test's own, tagging nothing but what the test passes it to."""
    identities = []

    def start() -> str:
        identity = leaked_processes.tag_run({})
        assert identity is not None
        identities.append(identity)
        return identity

    yield start
    for identity in identities:
        leaked_processes.end_run(identity)


def gone(process: psutil.Process) -> bool:
    try:
        return process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_a_run_stops_what_it_leaked_and_nothing_else(spawn, run):
    mine, other = spawn(run()), spawn(run())

    stopped = leaked_processes.reap_run(mine.environ()[RUN_ENV])

    assert [line.split()[0] for line in stopped] == [str(mine.pid)]
    assert gone(mine)
    assert not gone(other)


def test_processes_of_a_run_that_ended_are_stopped(spawn, run):
    ended = run()
    leaked_processes.end_run(ended)  # Its lock file goes with it.
    crashed = str(RUNS_DIR / f"crashed-{uuid.uuid4().hex}.lock")
    open(crashed, "w").close()  # Its file stays, but nobody holds the lock.
    try:
        orphans = [spawn(ended), spawn(crashed)]
        live = spawn(run())

        stopped = leaked_processes.reap_dead_runs([*orphans, live])

        # Another run starting now may reap the orphans first; either way they go.
        assert {int(line.split()[0]) for line in stopped} <= {p.pid for p in orphans}
        assert all(gone(process) for process in orphans)
        assert not gone(live)
    finally:
        os.unlink(crashed)


def test_only_a_run_proven_over_is_dead(run):
    assert leaked_processes._alive(run())
    assert leaked_processes._alive("not-a-tag")
    assert leaked_processes._alive("/elsewhere/run.lock")
    assert not leaked_processes._alive(str(RUNS_DIR / "never-existed.lock"))


def test_a_nested_run_takes_its_own_tag():
    environ = {RUN_ENV: "outer"}
    identity = leaked_processes.tag_run(environ)
    try:
        assert environ[RUN_ENV] == identity != "outer"
    finally:
        leaked_processes.end_run(identity)
