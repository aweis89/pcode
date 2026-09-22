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
from collections.abc import Iterable, Mapping
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

_SUPERVISOR = Path(__file__).with_name("_job_supervisor.py")


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


@dataclass
class Job:
    """One launched command and everything known about it without re-reading disk."""

    id: str
    command: str
    directory: Path
    supervisor_pid: int
    started_at: float
    background: bool
    pid: int | None = None
    exit_code: int | None = None
    ended_at: float | None = None
    stopped: bool = False
    # Set when a foreground wait was abandoned rather than completed, so the
    # next report can say why the model is holding a handle it did not ask for.
    detached: bool = False
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

    def summary(self) -> str:
        """One line naming the job, its command, and where it got to."""
        return f"[{self.id}] {self.command} → {self.outcome()} · {format_duration(self.elapsed)}"


class JobRegistry:
    """Launches jobs, tracks their exits, and owns their log directories."""

    def __init__(self, *, retain: int = RETAINED_FINISHED_JOBS) -> None:
        self.jobs: dict[str, Job] = {}
        self._retain = retain
        self._counter = 0
        self._waited: set[str] = set()
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
    ) -> Job:
        directory = Path(tempfile.mkdtemp(prefix="pcode-job-"))
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
        self._counter += 1
        job = Job(
            id=f"j{self._counter}",
            command=command,
            directory=directory,
            supervisor_pid=process.pid,
            started_at=time.time(),
            background=background,
        )
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
            if not job.running and channel not in job.announced and self._announceable(job)
        ]
        for job in pending:
            job.announced.add(channel)
        return pending

    def _announceable(self, job: Job) -> bool:
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
        """Kill the job's whole process group. Returns whether it was running."""
        self.refresh()
        if not job.running:
            return False
        _kill_session(job.supervisor_pid)
        job.stopped = True
        job.ended_at = time.time()
        return True

    def stop_all(self, jobs: Iterable[Job] | None = None) -> list[Job]:
        return [
            job for job in list(jobs if jobs is not None else self.jobs.values()) if self.stop(job)
        ]

    def _evict(self) -> None:
        finished = [job for job in self.jobs.values() if not job.running]
        for job in sorted(finished, key=lambda job: job.ended_at or 0)[: -self._retain or None]:
            shutil.rmtree(job.directory, ignore_errors=True)
            del self.jobs[job.id]

    def reset(self) -> None:
        """Stop everything and forget it. For tests, which share the registry.

        Deliberately harsher than `shutdown`: a test that leaked a process
        should not leave it running for the next one.
        """
        self.stop_all()
        for job in self.jobs.values():
            shutil.rmtree(job.directory, ignore_errors=True)
        self.jobs.clear()
        self._waited.clear()
        self._counter = 0
        self.cancel_policy = "detach"

    def shutdown(self) -> None:
        """Drop the logs of finished jobs; leave running ones and their logs alone.

        A job that outlives the session is the point of the design, so this
        must not kill anything. Its directory stays until the OS reclaims it,
        because the process is still writing there.
        """
        for job in list(self.jobs.values()):
            if not job.running:
                shutil.rmtree(job.directory, ignore_errors=True)
                del self.jobs[job.id]


def _kill_session(pid: int) -> None:
    if os.name == "nt":  # pragma: no cover -- no Windows CI.
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


_REGISTRY = JobRegistry()


def registry() -> JobRegistry:
    """The process-wide registry.

    Shared rather than per-run on purpose: a run is exactly the scope a job is
    supposed to escape, and the worker sub-agent should see -- and be able to
    stop -- the same jobs the parent started.
    """
    return _REGISTRY
