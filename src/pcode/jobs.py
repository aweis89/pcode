"""Session-scoped registry of shell jobs that outlive the agent run.

One execution mechanism, two ways to wait. Every command is launched the same
way -- a detached supervisor in its own session, writing a combined output log
and an atomic status file -- so "foreground" and "background" differ only in
whether the caller waits for the result, not in how the process runs. That is
what makes the interesting transitions cheap: a wait can be abandoned when the
user interrupts with a follow-up without disturbing the command, and a
backgrounded job can be waited on later without re-running anything.

Ownership is deliberately here rather than in the tool call: the tool call is
the thing that gets cancelled. A registry that outlives it can still say what
is running, deliver exits without the model polling, and clean up logs.

The registry also outlives pcode. Its state lives under the XDG state dir, one
directory per pcode process holding the job logs and a `registry.json`, so a
later pcode can adopt whatever a dead one left running instead of the user
hunting for it with `ps`.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

# Logs of finished jobs are kept so the model can re-read output it already
# summarized, but not forever: each job owns a directory, and a long session
# runs hundreds. Evicting the oldest finished job bounds the disk cost without
# ever deleting a log while its command is still writing to it.
RETAINED_FINISHED_JOBS = 50

# The model-visible tail of a job's log. Matches the window Harness used, which
# is sized for a tool result rather than for the whole log.
OUTPUT_TAIL_BYTES = 16_000

# A stop is SIGTERM first, so a server can release its port and a migration can
# roll back; only a command that ignores it for this long is killed.
STOP_GRACE_SECONDS = 2.0

_SUPERVISOR = Path(__file__).with_name("_job_supervisor.py")


def jobs_root() -> Path:
    """Where every pcode process keeps its job logs and registry record."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "jobs"


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Job:
    """One launched command and everything known about it without re-reading disk."""

    id: str
    command: str
    directory: Path
    supervisor_pid: int
    started_at: float
    background: bool
    # Why the model ran it, in its own words, for jobs it expects to come back
    # to. Empty for ordinary foreground commands, where the command is read
    # next to its own result and a label would only repeat it.
    purpose: str = ""
    pid: int | None = None
    exit_code: int | None = None
    ended_at: float | None = None
    stopped: bool = False
    # Set when a foreground wait was abandoned rather than completed, so the
    # next report can say why the model is holding a handle it did not ask for.
    detached: bool = False
    # Inherited from a pcode process that exited while this command ran. The
    # model of this session never launched it, so it is never woken for it.
    adopted: bool = False
    # True while a tool call is blocking on this job. The terminal uses it to
    # tell "in the background" from "what the live row already shows".
    waiting: bool = False
    announced: set[str] = field(default_factory=set)

    @property
    def output_path(self) -> Path:
        return self.directory / "output.log"

    @property
    def status_path(self) -> Path:
        return self.directory / "status.json"

    @property
    def running(self) -> bool:
        return self.exit_code is None and not self.stopped

    @property
    def elapsed(self) -> float:
        return (self.ended_at or time.time()) - self.started_at

    def outcome(self) -> str:
        if self.stopped:
            return "stopped"
        if self.exit_code is None:
            return "running"
        return f"exit {self.exit_code}"

    def label(self) -> str:
        """The shortest honest name for this job: its purpose, else its command."""
        return _clip(self.purpose or self.command, 80)

    def summary(self) -> str:
        """One line for the inventory view, where you decide what to stop.

        Unlike `label`, this keeps the command even when a purpose exists: a
        stated intention is not evidence of what is actually running.
        """
        detail = self.label()
        if self.purpose:
            detail += f" · {_clip(self.command, 60)}"
        line = f"[{self.id}] {detail} → {self.outcome()} · {format_duration(self.elapsed)}"
        if self.adopted:
            line += " · adopted from an earlier pcode"
        return line

    def record(self) -> dict:
        """What a later pcode needs to adopt this job: where it is, not what it said."""
        return {
            "command": self.command,
            "directory": str(self.directory),
            "supervisor_pid": self.supervisor_pid,
            "started_at": self.started_at,
            "purpose": self.purpose,
        }


class JobRegistry:
    """Launches jobs, tracks their exits, and owns their log directories."""

    def __init__(
        self,
        *,
        retain: int = RETAINED_FINISHED_JOBS,
        state: Callable[[], Path] | None = None,
    ) -> None:
        self.jobs: dict[str, Job] = {}
        self._retain = retain
        # Resolved lazily: the state dir comes from the environment, which
        # tests change after the process-wide registry has been created.
        self._state = state
        self._home: Path | None = None
        self._counter = 0
        self._waited: set[str] = set()
        # Jobs sent SIGTERM, by the time they get SIGKILL instead.
        self._terminating: dict[str, float] = {}
        # Ctrl+C means "stop what you are doing"; a typed follow-up means "stop
        # waiting, keep working". Only an abandoned wait consults this, so a
        # job the model explicitly backgrounded is never caught by either.
        self.cancel_policy = "detach"

    def launch(
        self,
        command: str,
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        background: bool = False,
        purpose: str = "",
    ) -> Job:
        self._counter += 1
        home = self._directory()
        if home is None:
            directory = Path(tempfile.mkdtemp(prefix="pcode-job-"))
        else:
            directory = home / f"j{self._counter}"
            directory.mkdir(parents=True, exist_ok=True)
        try:
            process = subprocess.Popen(
                [sys.executable, str(_SUPERVISOR), str(directory), command],
                cwd=cwd,
                env=None if env is None else dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        # Reap the supervisor without tying the command's lifetime to this
        # event loop: the thread is a daemon and only calls wait().
        threading.Thread(target=process.wait, daemon=True).start()
        job = Job(
            id=f"j{self._counter}",
            command=command,
            directory=directory,
            supervisor_pid=process.pid,
            started_at=time.time(),
            background=background,
            purpose=" ".join(purpose.split()),
        )
        self.jobs[job.id] = job
        self._save()
        return job

    def _directory(self) -> Path | None:
        """This process's own directory under the state root, or None when ephemeral."""
        if self._state is None:
            return None
        if self._home is None:
            self._home = self._state() / str(os.getpid())
        self._home.mkdir(parents=True, exist_ok=True)
        return self._home

    def _save(self) -> None:
        home = self._directory()
        if home is None:
            return
        record = {
            "owner_pid": os.getpid(),
            "jobs": {job.id: job.record() for job in self.jobs.values()},
        }
        pending = home / "registry.tmp"
        pending.write_text(json.dumps(record), encoding="utf-8")
        pending.replace(home / "registry.json")

    def adopt_orphans(self) -> list[Job]:
        """Take over the running jobs of pcode processes that are gone.

        Finished orphans are only logs nobody will read; their directories go
        with the dead process's record. A running one gets a fresh id here and
        keeps its directory, because its supervisor is still writing there.
        """
        if self._state is None:
            return []
        root = self._state()
        if not root.is_dir():
            return []
        adopted = []
        for home in sorted(root.iterdir()):
            if home.name == str(os.getpid()) or not home.is_dir():
                continue
            try:
                record = json.loads((home / "registry.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            owner = record.get("owner_pid") if isinstance(record, dict) else None
            if isinstance(owner, int) and _alive(owner):
                continue
            entries = record.get("jobs", {})
            kept = set()
            for entry in entries.values() if isinstance(entries, dict) else ():
                job = self._adopt(entry)
                if job is not None:
                    adopted.append(job)
                    kept.add(job.directory.resolve())
            for child in home.iterdir():
                if child.is_dir() and child.resolve() not in kept:
                    shutil.rmtree(child, ignore_errors=True)
                elif child.is_file():
                    child.unlink(missing_ok=True)
            _remove_if_empty(home)
        if adopted:
            self._save()
        return adopted

    def _adopt(self, entry: object) -> Job | None:
        if not isinstance(entry, dict):
            return None
        try:
            directory = Path(entry["directory"])
            supervisor_pid = int(entry["supervisor_pid"])
            command = str(entry["command"])
            started_at = float(entry["started_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if not directory.is_dir() or not _alive(supervisor_pid):
            return None
        self._counter += 1
        job = Job(
            id=f"j{self._counter}",
            command=command,
            directory=directory,
            supervisor_pid=supervisor_pid,
            started_at=started_at,
            # Nothing here is waiting on it, which is what background means.
            background=True,
            purpose=str(entry.get("purpose") or ""),
            adopted=True,
        )
        status = self._read_status(job)
        if status is not None:
            if status.get("exit_code") is not None:
                self._counter -= 1
                return None
            job.pid = status.get("pid")
        self.jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def running(self) -> list[Job]:
        self.refresh()
        return [job for job in self.jobs.values() if job.running]

    def refresh(self) -> list[Job]:
        """Read published statuses; return the jobs that finished since last time.

        Cheap enough to call on a UI tick: one small read per still-running job.
        """
        self._escalate()
        finished = []
        for job in list(self.jobs.values()):
            if not job.running:
                continue
            status = self._read_status(job)
            if status is None:
                continue
            if job.pid is None:
                job.pid = status.get("pid")
            if status.get("exit_code") is None:
                continue
            job.exit_code = status["exit_code"]
            job.ended_at = status.get("ended_at") or time.time()
            finished.append(job)
        if finished:
            self._evict()
        return finished

    def _escalate(self) -> None:
        """SIGKILL a stopped job that outlived its SIGTERM grace period."""
        for job_id, deadline in list(self._terminating.items()):
            job = self.jobs.get(job_id)
            status = self._read_status(job) if job is not None else None
            if job is None or (status is not None and status.get("exit_code") is not None):
                del self._terminating[job_id]
            elif time.time() >= deadline:
                _kill_session(job.supervisor_pid)
                del self._terminating[job_id]

    def _read_status(self, job: Job) -> dict | None:
        try:
            status = json.loads(job.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Absent until the supervisor's first publication, and never
            # partially readable: it is written to a temp file and renamed.
            return None
        return status if isinstance(status, dict) else None

    def take_announcements(self, channel: str) -> list[Job]:
        """Finished jobs this channel has not reported yet.

        Separate channels so the terminal and the model can each be told once.
        Only jobs the model was handed a handle for are worth announcing to it;
        a foreground command that completed inside its own tool call already
        reported itself.
        """
        self.refresh()
        pending = [
            job
            for job in self.jobs.values()
            if not job.running and channel not in job.announced and self.announceable(job)
        ]
        for job in pending:
            job.announced.add(channel)
        return pending

    def announceable(self, job: Job) -> bool:
        """Whether anyone was left holding a handle, so its exit is news."""
        return job.background or job.detached or job.stopped or job.id in self._waited

    def mark_waited(self, job: Job) -> None:
        """Record that a wait returned a handle instead of a result."""
        self._waited.add(job.id)

    def read_output(self, job: Job, *, max_bytes: int = OUTPUT_TAIL_BYTES) -> tuple[str, bool]:
        """The tail of the job's log, and whether anything was dropped before it."""
        try:
            size = job.output_path.stat().st_size
            with job.output_path.open("rb") as source:
                source.seek(max(0, size - max_bytes))
                data = source.read(max_bytes)
        except OSError:
            return "", False
        return data.decode("utf-8", errors="replace"), size > len(data)

    def stop(self, job: Job) -> bool:
        """Stop the job's whole process group. Returns whether it was running.

        The group gets SIGTERM now and SIGKILL from a later `refresh` if it is
        still there after the grace period. Nothing blocks on it: the caller
        may be a cancellation handler on the event loop.
        """
        return bool(self.stop_all([job]))

    def stop_all(self, jobs: Iterable[Job] | None = None) -> list[Job]:
        self.refresh()
        stopped = []
        for job in list(jobs if jobs is not None else self.jobs.values()):
            if not job.running:
                continue
            _terminate_session(job.supervisor_pid)
            job.stopped = True
            job.ended_at = time.time()
            self._terminating[job.id] = time.time() + STOP_GRACE_SECONDS
            stopped.append(job)
        if stopped:
            self._save()
        return stopped

    def _evict(self) -> None:
        finished = [job for job in self.jobs.values() if not job.running]
        evicted = sorted(finished, key=lambda job: job.ended_at or 0)[: -self._retain or None]
        for job in evicted:
            shutil.rmtree(job.directory, ignore_errors=True)
            _remove_if_empty(job.directory.parent)
            del self.jobs[job.id]
        if evicted:
            self._save()

    def reset(self) -> None:
        """Stop everything and forget it. For tests, which share the registry.

        Deliberately harsher than `shutdown`: a test that leaked a process
        should not leave it running for the next one.
        """
        for job in self.jobs.values():
            _kill_session(job.supervisor_pid)
            shutil.rmtree(job.directory, ignore_errors=True)
        self.jobs.clear()
        self._waited.clear()
        self._terminating.clear()
        self._counter = 0
        self.cancel_policy = "detach"
        if self._home is not None:
            shutil.rmtree(self._home, ignore_errors=True)
            self._home = None

    def shutdown(self) -> None:
        """Drop the logs of finished jobs; leave running ones and their logs alone.

        A job that outlives the session is the point of the design, so this
        must not kill anything. Its directory and the registry record stay so
        the next pcode can adopt it; both go once nothing is left running.
        """
        for job in list(self.jobs.values()):
            if not job.running:
                shutil.rmtree(job.directory, ignore_errors=True)
                del self.jobs[job.id]
        if self._home is None:
            return
        if self.jobs:
            self._save()
        else:
            shutil.rmtree(self._home, ignore_errors=True)
            self._home = None


def _kill_session(pid: int) -> None:
    _signal_session(pid, signal.SIGKILL)


def _terminate_session(pid: int) -> None:
    _signal_session(pid, signal.SIGTERM)


def _signal_session(pid: int, signum: int) -> None:
    if os.name == "nt":  # pragma: no cover -- no Windows CI.
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
        return
    try:
        os.killpg(pid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _remove_if_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


_REGISTRY = JobRegistry(state=jobs_root)


def registry() -> JobRegistry:
    """The process-wide registry.

    Shared rather than per-run on purpose: a run is exactly the scope a job is
    supposed to escape, and the worker sub-agent should see -- and be able to
    stop -- the same jobs the parent started.
    """
    return _REGISTRY
