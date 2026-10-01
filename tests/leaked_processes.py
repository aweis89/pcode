"""Find and stop processes a test run left behind.

A test that starts a process with `start_new_session`, or behind a shell's
`( ... &)`, hands it to init: it is no longer pytest's descendant and survives
the run, spinning or holding memory until someone notices. The environment is
the one thing every such process still inherits, so the run's controller tags
itself with `RUN_ENV` before xdist starts workers, and each descendant carries
the tag however it detached.

At the end of a run the controller stops everything still tagged with its own
id. A run that died before its teardown (SIGKILL, a stopped job) cannot, so
each run first stops processes tagged by a run whose controller is gone.
Processes whose environment macOS hides (Apple platform binaries such as
`/bin/sh` and `/bin/sleep`, so a bare shell loop) are invisible to this; the
leaks that matter here are Python and tmux, which are not.
"""

import os
import signal

import psutil

RUN_ENV = "PCODE_TEST_RUN"
# How long a tagged process gets to exit on SIGTERM before SIGKILL.
TERM_GRACE_SECONDS = 3.0


def run_id(process: psutil.Process) -> str:
    return f"{process.pid}-{process.create_time():.3f}"


def tag_run() -> str:
    """Tag this process, and so everything it starts, as a fresh run.

    Overwrites an inherited tag: a pytest started by a test is a run of its
    own, and must not reap the outer run's workers when it finishes.
    """
    os.environ[RUN_ENV] = run_id(psutil.Process())
    return os.environ[RUN_ENV]


def _alive(identity: str) -> bool:
    """Whether the run's controller may still be running.

    Only proof of death counts: the pid is gone, or now names a different
    process. Anything short of that (a malformed tag, a refused lookup) keeps
    the run alive, since reaping a live run kills its workers mid-test.
    """
    try:
        pid = int(identity.partition("-")[0])
    except ValueError:
        return True
    try:
        return run_id(psutil.Process(pid)) == identity
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return True


def _tagged(keep, candidates=None) -> list[psutil.Process]:
    me = os.getpid()
    found = []
    for process in psutil.process_iter() if candidates is None else candidates:
        if process.pid == me:
            continue
        try:
            identity = process.environ().get(RUN_ENV)
        except psutil.Error:
            continue
        if identity and not keep(identity):
            found.append(process)
    return found


def _stop(processes: list[psutil.Process]) -> list[str]:
    described = []
    for process in processes:
        try:
            command = " ".join(process.cmdline())[:120]
        except psutil.Error:
            command = "?"
        described.append(f"{process.pid} {command}")
        try:
            process.send_signal(signal.SIGTERM)
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(processes, timeout=TERM_GRACE_SECONDS)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            pass
    return described


def reap_run(identity: str) -> list[str]:
    """Stop every process still tagged with this run; describe what was stopped."""
    return _stop(_tagged(keep=lambda tag: tag != identity))


def reap_dead_runs(candidates=None) -> list[str]:
    """Stop processes tagged by runs whose controller has exited.

    `candidates` limits the search, so a test can check this without reaping
    the whole machine from inside a worker.
    """
    return _stop(_tagged(keep=_alive, candidates=candidates))
