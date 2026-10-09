import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

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
        if identity is None:
            pytest.skip(f"cannot take a run lock under {RUNS_DIR}")
        identities.append(identity)
        return identity

    yield start
    for identity in identities:
        leaked_processes.end_run(identity)


@pytest.fixture
def crashed_run():
    """A run whose controller took its lock and was then SIGKILLed, skipping teardown."""
    tests = Path(__file__).parent
    script = (
        f"import sys, time; sys.path.insert(0, {str(tests)!r}); import leaked_processes; "
        "print(leaked_processes.tag_run({}), flush=True); time.sleep(60)"
    )
    holder = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    identity = ""
    try:
        ready, _, _ = select.select([holder.stdout], [], [], 30)
        identity = holder.stdout.readline().strip() if ready else ""
        if identity == "None":
            pytest.skip(f"cannot take a run lock under {RUNS_DIR}")
        assert Path(identity).parent == RUNS_DIR, (
            f"lock holder printed {identity!r} (exit {holder.poll()})"
        )
        assert leaked_processes._alive(identity)  # Held by another process.
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        yield identity
    finally:
        holder.kill()
        holder.wait()
        holder.stdout.close()
        if identity and Path(identity).parent == RUNS_DIR:
            Path(identity).unlink(missing_ok=True)


def gone(process: psutil.Process) -> bool:
    try:
        return process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_a_run_stops_what_it_leaked_and_nothing_else(spawn, run):
    identity = run()
    mine, other = spawn(identity), spawn(run())

    # Not `mine.environ()` again: macOS can fail that read of a live process
    # transiently (sysctl KERN_PROCARGS2 -> EIO), and `spawn` already saw the tag.
    stopped = leaked_processes.reap_run(identity)

    assert [line.split()[0] for line in stopped] == [str(mine.pid)]
    assert gone(mine)
    assert not gone(other)


def test_processes_of_a_run_that_crashed_are_stopped(spawn, run, crashed_run):
    assert not leaked_processes._alive(crashed_run)
    orphan, live = spawn(crashed_run), spawn(run())

    stopped = leaked_processes.reap_dead_runs([orphan, live])

    # Another run starting now may reap the orphan first; either way it goes.
    assert {line.split()[0] for line in stopped} <= {str(orphan.pid)}
    assert gone(orphan)
    assert not gone(live)


def test_a_transiently_unreadable_environment_is_read_again(run, crashed_run, monkeypatch):
    """macOS can fail the read of one of our own live processes once (EIO, which
    psutil reports as AccessDenied); that must not hide a dead run's orphan."""

    class Flaky:
        def __init__(self, pid, tag, failures, uid=os.getuid()):
            self.pid, self.tag, self.failures, self.uid = pid, tag, failures, uid

        def environ(self):
            if self.failures:
                self.failures -= 1
                raise psutil.AccessDenied(self.pid)
            return {RUN_ENV: self.tag}

        def uids(self):
            return SimpleNamespace(real=self.uid)

    monkeypatch.setattr(leaked_processes, "ENVIRON_RETRY_SECONDS", 0)
    orphan = Flaky(-1, crashed_run, failures=1)
    foreign = Flaky(-2, crashed_run, failures=99, uid=os.getuid() + 1)
    live = Flaky(-3, run(), failures=1)

    found = leaked_processes._tagged(leaked_processes._alive, [orphan, foreign, live])

    assert found == [orphan]
    assert foreign.failures == 98  # Never retried: another user's stays denied.


def test_only_a_run_proven_over_is_dead(run):
    assert leaked_processes._alive(run())
    assert leaked_processes._alive("not-a-tag")
    assert leaked_processes._alive("/elsewhere/run.lock")
    # A missing file proves nothing: a reaper with a private /tmp sees none.
    assert leaked_processes._alive(str(RUNS_DIR / "never-existed.lock"))


def test_a_nested_run_takes_its_own_tag_and_gives_the_outer_one_back(run):
    environ = {RUN_ENV: "outer"}
    identity = leaked_processes.tag_run(environ)
    if identity is None:
        pytest.skip(f"cannot take a run lock under {RUNS_DIR}")
    try:
        assert environ[RUN_ENV] == identity != "outer"
    finally:
        leaked_processes.end_run(identity)
    assert environ[RUN_ENV] == "outer"
    assert not Path(identity).exists()
