"""The terminal's side of a session host (see `pcode.host`).

`RemoteRuntime` stands in for `AgentRuntime` while another process runs the
agent: `stream` sends a prompt and yields the host's events, `follow` yields a
turn this terminal did not start (another terminal's, steering the last turn
never took, or the one running when it attached). Everything the app would
otherwise reach into the runtime for (`agent`, `jobs`, `mcp`, `tree`) is
absent, so those features report themselves unavailable instead of acting on
a conversation that lives elsewhere.
"""

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from pcode.host_protocol import (
    LINE_LIMIT,
    PROTOCOL,
    HostEntry,
    decode_event,
    dumps,
    host_dir,
    list_hosts,
    read_message,
    socket_path,
)


class HostError(RuntimeError):
    """The host could not be reached, refused this terminal, or went away."""

    # Carries no provider text, so it is shown as is.
    sanitized = True


class TurnFailed(RuntimeError):
    """The host's turn failed; the message was already sanitized there."""

    sanitized = True


class HostTurnCancelled(Exception):
    """Something other than this terminal cancelled the turn it was following."""


class RemoteRuntime:
    remote = True
    agent = None
    jobs = None
    mcp = None
    tree = None
    recovery_blocked = None
    session = None
    session_factory = None

    def __init__(self, reader, writer, welcome: dict, snapshot: dict) -> None:
        self.id: str = welcome["id"]
        self.pid: int = welcome["pid"]
        self.model: str = welcome["model"]
        self.workspace = Path(welcome["workspace"]).resolve()
        self.session_id: str = snapshot.get("session_id") or welcome.get("session_id") or ""
        self.snapshot = snapshot
        self._startup_context: list[str] = welcome.get("startup_context") or []
        self.context = welcome.get("context", "")
        self.effort: str = welcome.get("effort", "default")
        self.turns = 0
        # Set by the app, as it sets them on AgentRuntime.
        self.compaction_notice: Callable[[str], None] = lambda text: None
        self.retry_notice: Callable[[str], None] = lambda text: None
        self.warning_notice: Callable[[str], None] = lambda text: None
        self.take_steering: Callable[[], list[str]] = lambda: []
        # Set by the app: a turn started that nobody here is following yet.
        self.on_turn: Callable[[str, bool], None] | None = None
        self.on_notice: Callable[[str], None] | None = None
        self.on_closed: Callable[[], None] | None = None
        self.on_session: Callable[[str], None] | None = None
        # What an abandoned shell wait should do on the host; see set_cancel_policy.
        self.cancel_policy = "stop"
        # Leaving for another host: the turn keeps running there, unannounced.
        self.detaching = False
        self._left = False
        self.closed = False
        # The host went away on its own, rather than this terminal leaving it.
        self.lost = False
        self._reader, self._writer = reader, writer
        # Where the reader routes the running turn's messages, and a turn
        # started elsewhere that `follow` has not picked up yet.
        self._inbox: asyncio.Queue | None = None
        # The prompt the inbox waits on; None takes the first turn that ends.
        self._inbox_request: str | None = None
        self._unclaimed: asyncio.Queue | None = None
        self.pending_turn: dict | None = None
        # This terminal already showed steering it forwarded, so the turn that
        # steering starts when it arrives too late is not echoed a second time.
        self._steered = False
        if turn := snapshot.get("turn"):
            queue: asyncio.Queue = asyncio.Queue()
            queue.put_nowait({"type": "turn_started", **turn, "attached": True})
            for message in turn.get("messages", []):
                queue.put_nowait(message)
            self._inbox = self._unclaimed = queue
            self.pending_turn = {"prompt": turn["prompt"], "echo": True}
        self._task = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, path: Path) -> "RemoteRuntime":
        try:
            reader, writer = await asyncio.open_unix_connection(path, limit=LINE_LIMIT)
        except OSError as error:
            raise HostError(f"Cannot reach the session host at {path}: {error.strerror}") from None
        writer.write(dumps({"type": "hello", "protocol": PROTOCOL}))
        await writer.drain()
        welcome = await read_message(reader)
        if welcome is None:
            raise HostError("The session host closed the connection.")
        if welcome.get("type") == "error":
            raise HostError(welcome.get("message", "The session host refused this terminal."))
        snapshot = await read_message(reader)
        if snapshot is None or snapshot.get("type") != "snapshot":
            raise HostError("The session host sent no conversation.")
        return cls(reader, writer, welcome, snapshot)

    # What the app asks of a runtime

    def startup_context(self) -> list[str]:
        return list(self._startup_context)

    def reset(self) -> None:
        raise ValueError("/new is not available in a session host; use /switch new.")

    def close(self) -> None:
        """Detach: the host and its turn carry on without this terminal."""
        self.detaching = True
        if self._left:
            return  # The app detaches before its loop ends, and main() again after.
        self._left = True
        self._task.cancel()
        try:
            self._writer.close()
        except (OSError, RuntimeError):
            pass

    def stop(self, *, keep_worktree: bool = False) -> None:
        """Ask the host to finish, then detach.

        `keep_worktree` leaves the worktree for this terminal to tidy (asking
        first) or for a restarted host to resume in.
        """
        self._send({"type": "stop", "keep_worktree": keep_worktree})
        self.close()

    async def hand_off(self, prompt: str) -> None:
        """Start a turn and leave it running, for a session begun in the background."""
        self._send({"type": "prompt", "text": prompt, "request": uuid4().hex})
        try:
            await self._writer.drain()
        except (OSError, RuntimeError):
            pass
        self.close()

    def release_waits(self) -> None:
        """Hand steering typed during the turn to the host now.

        The app calls this as a steering message is submitted. A local runtime
        pulls steering at its next model request; the host does the same with
        what arrives here, so it is forwarded at once rather than waited for.
        """
        messages = self.take_steering()
        if messages:
            self._steered = True
            self._send({"type": "steer", "messages": messages})

    async def stream(self, prompt: str | None) -> AsyncIterator:
        if prompt is None:
            raise ValueError("/resend is not available in a session host yet.")
        if self.closed:
            raise HostError("The session host has exited.")
        if self._unclaimed is not None:
            # A turn started elsewhere is still running; show it, then send.
            async for event in self.follow():
                yield event
        request = uuid4().hex
        queue: asyncio.Queue = asyncio.Queue()
        self._inbox, self._inbox_request = queue, request
        self._steered = False
        self._send({"type": "prompt", "text": prompt, "request": request})
        async for event in self._consume(queue, request):
            yield event

    async def follow(self) -> AsyncIterator:
        queue, self._unclaimed = self._unclaimed, None
        self.pending_turn = None
        if queue is None:
            return
        async for event in self._consume(queue, None):
            yield event

    async def _consume(self, queue: asyncio.Queue, request: str | None) -> AsyncIterator:
        # Waiting on our own prompt, messages of a turn that finishes first
        # (one cancelled a moment ago, say) are not ours to show.
        started = request is None
        finished = False
        try:
            while True:
                message = await queue.get()
                kind = message.get("type")
                if kind == "closed":
                    finished = True
                    raise HostError("The session host has exited.")
                if kind == "turn_started":
                    started = started or message.get("request") == request
                    continue
                if not started:
                    continue
                if kind == "event":
                    yield decode_event(message["event"])
                elif kind == "notice":
                    self._notice(message)
                elif kind == "turn_finished":
                    if request is not None and message.get("request") != request:
                        continue
                    finished = True
                    self.turns += 1
                    self.context = message.get("context", self.context)
                    self.recovery_blocked = message.get("recovery_blocked") or None
                    if message.get("outcome") == "failed":
                        raise TurnFailed(message.get("error") or "The turn failed on the host.")
                    if message.get("outcome") == "cancelled":
                        raise HostTurnCancelled()
                    return
        finally:
            if self._inbox is queue:
                self._inbox = None
            if not finished and not self.detaching and not self.closed:
                # Ctrl+C, or a typed interrupt, here: stop the host's turn too.
                self._send({"type": "cancel", "policy": self.cancel_policy})

    # Connection

    def _send(self, message: dict) -> None:
        if self.closed:
            return
        try:
            self._writer.write(dumps(message))
        except (OSError, RuntimeError):
            self.closed = True

    def _notice(self, message: dict) -> None:
        text, level = message.get("text", ""), message.get("level", "note")
        if level == "warning":
            self.warning_notice(text)
        elif level == "compaction":
            self.compaction_notice(text)
        elif level == "retry":
            self.retry_notice(text)
        elif self.on_notice is not None:
            self.on_notice(text)

    async def _read(self) -> None:
        try:
            while (message := await read_message(self._reader)) is not None:
                self._dispatch(message)
        except (ValueError, OSError):
            pass
        finally:
            self.closed = True
            self.lost = not self.detaching
            if self._inbox is not None:
                self._inbox.put_nowait({"type": "closed"})
            if self.on_closed is not None and self.lost:
                self.on_closed()

    def _dispatch(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "session":
            self.session_id = message.get("session_id", self.session_id)
            if self.on_session is not None:
                self.on_session(self.session_id)
            return
        if kind == "turn_started" and self._inbox is None:
            # Nobody asked for this turn: queue it for `follow`, which the app
            # starts from its own queue so it runs in order with typed prompts.
            queue: asyncio.Queue = asyncio.Queue()
            self._inbox = self._unclaimed = queue
            self._inbox_request = None
            echo = not (message.get("source") == "steering" and self._steered)
            self._steered = False
            self.pending_turn = {"prompt": message.get("prompt", ""), "echo": echo}
            queue.put_nowait(message)
            if self.on_turn is not None:
                self.on_turn(message.get("prompt", ""), echo)
            return
        if self._inbox is not None and kind in {
            "turn_started",
            "event",
            "notice",
            "steering_taken",
            "turn_finished",
        }:
            self._inbox.put_nowait(message)
            ours = self._inbox_request in (None, message.get("request"))
            if kind == "turn_finished" and ours:
                # Whatever starts next goes to a new queue, never behind this
                # end. The end of a turn cancelled a moment ago is not ours: the
                # prompt sent since is still to start.
                self._inbox = None
                self.context = message.get("context", self.context)
            return
        if kind == "notice":
            self._notice(message)

    def overview(self) -> list[tuple[str, str]]:
        """`/status` rows for a conversation that lives in a host."""
        rows = [
            ("Model", self.model),
            ("Workspace", str(self.workspace)),
            ("Host", f"{self.id} · pid {self.pid}"),
            ("Session", self.session_id or "Will be saved after the first prompt."),
            ("Turns (this terminal)", str(self.turns)),
        ]
        if self.context:
            rows.append(("Context", self.context.strip(" ·")))
        return rows


class HostView:
    """The attach snapshot, shaped like the `SavedSession` that `PreviewApp.replay` reads."""

    forked_from = None

    def __init__(self, runtime: RemoteRuntime) -> None:
        self.info = SimpleNamespace(id=runtime.session_id or runtime.id)
        self._snapshot = runtime.snapshot

    def latest_plan(self) -> list[dict]:
        return list(self._snapshot.get("plan") or [])

    def transcript_records(self):
        return iter(self._snapshot.get("records") or [])


@dataclass
class HostLaunch:
    """A host to connect to once the terminal's event loop runs: just spawned, or running."""

    id: str
    process: subprocess.Popen | None = None
    log: Path | None = None
    directory: Path | None = None

    async def connect(self) -> RemoteRuntime:
        return await wait_for_host(self.id, self.process, self.log, directory=self.directory)

    @classmethod
    def running(cls, entry: HostEntry) -> "HostLaunch":
        return cls(entry.id, log=Path(entry.log) if entry.log else None)


def spawn_host(
    *,
    model: str,
    workspace: Path,
    resume: str | None = None,
    session_dir: Path | None = None,
    no_save: bool = False,
    worktree=None,
    no_worktree: bool = False,
    directory: Path | None = None,
) -> tuple[str, subprocess.Popen, Path]:
    """Start a host from this process, so it inherits this terminal's environment."""
    directory = directory or host_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity = uuid4().hex[:8]
    socket_path(identity, directory)  # Fail here, not in the child, on a too-long path.
    log = directory / f"{identity}.log"
    argv = [
        sys.executable,
        "-m",
        "pcode.host",
        "--id",
        identity,
        "--model",
        model,
        "--workspace",
        str(workspace),
    ]
    if resume:
        argv += ["--resume", resume]
    if session_dir:
        argv += ["--session-dir", str(session_dir)]
    if no_save:
        argv.append("--no-save")
    if no_worktree:
        argv.append("--no-worktree")
    elif isinstance(worktree, str):
        argv += ["--worktree", worktree]
    elif worktree:
        argv.append("--worktree")
    env = {**os.environ, "PCODE_HOST_LOG": str(log), "PCODE_HOST_DIR": str(directory)}
    fd = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "ab") as output:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            cwd=workspace,
            env=env,
            # Its own session: closing this terminal's tab does not signal it.
            start_new_session=True,
        )
    return identity, process, log


async def wait_for_host(
    identity: str,
    process: subprocess.Popen | None = None,
    log: Path | None = None,
    *,
    directory: Path | None = None,
    timeout: float = 300.0,
) -> RemoteRuntime:
    """Connect once the host is listening. Worktree setup can make that a while."""
    path = socket_path(identity, directory)
    deadline = time.monotonic() + timeout
    while True:
        if process is not None and process.poll() is not None:
            raise HostError(_exit_message(process.returncode, log))
        if path.exists():
            try:
                return await RemoteRuntime.connect(path)
            except HostError:
                if process is None:
                    raise
        if time.monotonic() > deadline:
            raise HostError(f"The session host did not start within {timeout:.0f}s; see {log}.")
        await asyncio.sleep(0.05)


def _exit_message(code: int, log: Path | None) -> str:
    tail = ""
    if log is not None:
        try:
            lines = log.read_text(errors="replace").strip().splitlines()
            tail = "\n".join(lines[-8:])
        except OSError:
            pass
    message = f"The session host exited during startup (status {code})."
    if tail:
        message += f"\n{tail}"
    if log is not None:
        message += f"\nLog: {log}"
    return message


def others(current: str | None, directory: Path | None = None) -> list[HostEntry]:
    return [entry for entry in list_hosts(directory) if entry.id != current]


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def wait_for_exit(pid: int, timeout: float = 30.0) -> None:
    """Until the host has let go of its session: the next host can then open it."""
    deadline = time.monotonic() + timeout
    while _running(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


def wait_for_exit_sync(pid: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while _running(pid) and time.monotonic() < deadline:
        time.sleep(0.05)


async def stop_entry(entry: HostEntry) -> None:
    runtime = await RemoteRuntime.connect(entry.socket)
    runtime.stop()
    await wait_for_exit(entry.pid)
