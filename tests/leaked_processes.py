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

A run is alive while its controller holds an exclusive `flock` on the file its
tag names; the kernel drops the lock the moment the controller dies, however it
dies. Not a pid and start time: psutil's `create_time()` on macOS shifts with
`kern.boottime`, so after a clock step two processes disagree about the same
controller, and a live run's workers were once reaped that way.
"""

import fcntl
import os
import signal
import time
import uuid
from pathlib import Path

import psutil

RUN_ENV = "PCODE_TEST_RUN"
# Fixed, not tempfile.gettempdir(): tests and nested runs change TMPDIR, and
# every run must look for a lock where its owner put it. The tag is the path.
RUNS_DIR = Path("/tmp") / f"pcode-test-runs-{os.getuid()}"
# How long a tagged process gets to exit on SIGTERM before SIGKILL.
TERM_GRACE_SECONDS = 3.0
# A lock file is created a moment before it is locked; leave young ones alone.
STALE_LOCK_SECONDS = 60.0

# Runs this process owns, by tag: the lock descriptor, and the environment it
# tagged with the tag that was there before.
_held: dict[str, tuple[int, object, str | None]] = {}


def tag_run(environ=os.environ) -> str | None:
    """Start a run: hold its lock and tag `environ`, so everything started after carries it.

    Overwrites an inherited tag: a pytest started by a test is a run of its
    own, and must not reap the outer run's workers when it finishes. Returns
    None, leaving reaping off for the run, if the lock cannot be taken.
    """
    path = str(RUNS_DIR / f"{uuid.uuid4().hex}.lock")
    try:
        RUNS_DIR.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        Path(path).unlink(missing_ok=True)
        return None
    _held[path] = (fd, environ, environ.get(RUN_ENV))
    environ[RUN_ENV] = path
    return path


def end_run(identity: str) -> None:
    """Release a run's lock, remove its file, and put back the tag it replaced."""
    held = _held.pop(identity, None)
    if held is None:
        return
    fd, environ, previous = held
    if environ.get(RUN_ENV) == identity:
        if previous is None:
            environ.pop(RUN_ENV, None)
        else:
            environ[RUN_ENV] = previous
    Path(identity).unlink(missing_ok=True)
    os.close(fd)


def _alive(identity: str) -> bool:
    """Whether the run may still be going. Only proof of its end counts as dead.

    The proof is a lock file nobody holds. A missing file proves nothing: a
    reaper with a private /tmp sees no one's files, and a run that ended
    cleanly has already reaped its own processes.
    """
    path = Path(identity)
    if path.parent != RUNS_DIR:
        return True
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True  # Held (or unknowable): the controller is still there.
    finally:
        os.close(fd)
    return False


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
    """Stop processes tagged by runs that have ended, and drop those runs' lock files.

    `candidates` limits the search, so a test can check this without reaping
    the whole machine from inside a worker.
    """
    stopped = _stop(_tagged(keep=_alive, candidates=candidates))
    if candidates is None and RUNS_DIR.is_dir():
        now = time.time()
        for path in RUNS_DIR.glob("*.lock"):
            try:
                young = now - path.stat().st_mtime < STALE_LOCK_SECONDS
            except OSError:
                continue
            if not young and not _alive(str(path)):
                path.unlink(missing_ok=True)
    return stopped
