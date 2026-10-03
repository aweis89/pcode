"""Session hosts for clients with no terminal: start one, send it a message, stop it.

`pcode --attach HOST --print` and the email listener (`pcode.email_remote`) both
drive hosts without an editor. What they share lives here: attaching as a
headless caller, sending one message and following only its own turn (the host
says when it starts, or that it was dropped, in order with what it shows; see
`pcode.host._Owner`), and detaching. Each client brings a `HeadlessView`
subclass for what to do with the turn's output.

The rest is the surface a remote control needs:

    start_session(workspace, profile=..., resume=None) -> HostEntry
    send(entry, text) -> TurnResult        # waits for the message's turn to end
    status(entry) -> SessionStatus
    stop(entry)                            # cancels the turn and the host's queue
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pcode.host_protocol import HostEntry, find_host, socket_path
from pcode.remote import HostError, RemoteController, notify_host, spawn_host
from pcode.remote_profile import LIMIT_PREFIX, RemoteProfile
from pcode.rpc import RemoteError
from pcode.runtime import Message, TextDelta
from pcode.ui import Activity

# How a message's turn ended, as `TurnResult.outcome` reports it.
COMPLETED, FAILED, CANCELLED, LIMIT = "completed", "failed", "cancelled", "limit"


class HeadlessView:
    """The view a headless caller gives the host: its own turn or command, nothing else.

    Tracks whether this caller's message or command is the one running, and
    how it ended. What to do with its output is the subclass's: `event` for
    each turn event, `scrollback` for notes, warnings and errors, `retry` when
    a provider request is retried, `settle` when a reply block must be closed,
    and `gone` when the host went away first.
    """

    # Scrollback passed on while this caller's own turn or command runs.
    SCROLLBACK = frozenset(
        {"user", "note", "retained_note", "flash", "cancelled", "tool_result", "shell_result"}
    )

    def __init__(self) -> None:
        # The host's live-panel fields, mirrored by the RemoteController.
        self.activity = Activity()
        # Whether the message's turn is running, or the command sent is.
        self.turn = False
        self.command = False
        self.failed = False
        # The message was dropped from the host's queue without running.
        self.dropped = False
        # The command sent ended the session, so the host stopping is its doing.
        self.ended = False
        # How the turn ended: the host's prompt state ("done", "failed", "cancelled").
        self.prompt_state = ""
        self.finished = asyncio.Event()

    # What a subclass does with the output

    def event(self, event) -> None:
        pass

    def retry(self, text: str) -> None:
        pass

    def settle(self) -> None:
        pass

    def scrollback(self, name: str, args: tuple, kwargs: dict) -> None:
        pass

    def gone(self, *, stopped: bool) -> None:
        pass

    # What the host says about this caller's own message or command

    @property
    def showing(self) -> bool:
        return self.turn or self.command

    def message_started(self) -> None:
        self.turn = True

    def message_dropped(self) -> None:
        self.dropped = self.failed = True
        self.finished.set()

    def session_ended(self) -> None:
        self.ended = True

    def host_gone(self, *, stopped: bool) -> None:
        if self.finished.is_set():
            return
        self.settle()
        if not self.ended:
            self.failed = True
        self.gone(stopped=stopped)
        self.finished.set()

    # The turn

    def turn_event(self, event) -> None:
        if self.turn:
            self.event(event)

    def turn_retry(self, text: str) -> None:
        if self.turn:
            self.retry(text)

    def turn_ended(self) -> None:
        if self.turn:
            self.settle()

    async def after_turn(self) -> None:
        if self.turn:
            self.turn = False
            self.prompt_state = self.activity.prompt_state
            self.failed = self.failed or self.prompt_state != "done"
            self.finished.set()

    # Scrollback

    def _scrollback(self, name: str, *args, **kwargs) -> None:
        if self.showing:
            self.settle()
            self.scrollback(name, args, kwargs)

    def warning(self, text: str) -> None:
        self._scrollback("warning", text)
        # A command's warning is a refusal ("unavailable while working"), which
        # a caller must not read as success. A turn's is advice; how the turn
        # ended says whether it worked.
        self.failed = self.failed or self.command

    def error(self, text: str, *, title: str = "Error") -> None:
        self._scrollback("error", text, title=title)
        self.failed = self.failed or self.command

    def show_events(self, events) -> None:
        self._scrollback("events", tuple(events))

    # What the host hands back to the terminal that sent a command

    async def run_command(self, text: str, *, idle: bool, tag) -> None:
        # Terminal commands never reach the host from here, so this is one it lacks.
        self.error(f"Unknown command {text.split(maxsplit=1)[0]}.")

    async def _popup(self, *args):
        self.error("This command opens a picker, which needs the interactive terminal.")
        return None

    choose_model = read_asides = browse_jobs = _popup

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self.SCROLLBACK:
            return lambda *args, **kwargs: self._scrollback(name, *args, **kwargs)
        # Anything else repaints a live panel this caller does not have.
        return lambda *args, **kwargs: None


@asynccontextmanager
async def attached(entry: HostEntry, view: HeadlessView):
    """Attach to the host as a headless caller; what it showed before is not ours."""
    controller, welcome = await RemoteController.connect(entry.socket, view, view.activity)
    # Wired before anything else is awaited, so no close goes unnoticed.
    controller.on_closed = lambda: view.host_gone(stopped=controller.host_stopped)
    if controller.peer.closed.is_set():
        controller.on_closed()
    try:
        await controller.start({**welcome, "calls": []})
        yield controller
    finally:
        await controller.detach()


async def host_call(controller: RemoteController, method: str, text: str, view: HeadlessView):
    """A call only newer hosts have; None when the host went away (and has said so)."""
    try:
        return await controller.peer.request(method, text)
    except RemoteError as error:
        if error.type_name == "PermissionError":
            raise HostError(
                "The session host runs older pcode; /restart it to send it messages headlessly."
            ) from error
        raise
    except ConnectionError:
        view.host_gone(stopped=controller.host_stopped)
        return None


async def submit(controller: RemoteController, text: str, view: HeadlessView) -> None:
    """Queue `text` behind whatever the host is doing, and wait for its turn to end."""
    await host_call(controller, "send", text, view)
    await view.finished.wait()


# The remote-control surface


@dataclass
class ChangeSummary:
    """Where a session's work is, and what it changed since `base`."""

    worktree: str
    branch: str
    diff_stat: str
    untracked: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [f"Worktree: {self.worktree}", f"Branch: {self.branch or '(detached)'}"]
        if self.diff_stat:
            lines += ["", self.diff_stat]
        if self.untracked:
            shown = self.untracked[:20]
            more = len(self.untracked) - len(shown)
            lines += ["", "New files:", *(f"  {path}" for path in shown)]
            if more:
                lines.append(f"  … and {more} more")
        if not self.diff_stat and not self.untracked:
            lines.append("No changes.")
        return "\n".join(lines)


@dataclass
class TurnResult:
    """How one message's turn went: its reply, how it ended, and what it changed."""

    reply: str
    outcome: str
    # Warnings and errors the turn showed, in order.
    notes: list[str] = field(default_factory=list)
    changes: ChangeSummary | None = None


@dataclass
class SessionStatus:
    host: str
    session_id: str
    # "starting", "idle", "working", or "stopped" (no host running).
    state: str
    title: str = ""
    last_prompt: str = ""
    turns: int = 0
    outcome: str = ""
    workspace: str = ""
    attached: int = 0


class CollectView(HeadlessView):
    """Keeps the turn's reply text and its warnings, for a client that sends them on."""

    def __init__(self) -> None:
        super().__init__()
        self.block = ""
        self.messages: list[str] = []
        self.notes: list[str] = []
        self.limit = ""

    def event(self, event) -> None:
        if isinstance(event, TextDelta):
            self.block += event.text
        elif isinstance(event, Message):
            text = event.markdown or self.block
            if text.strip():
                self.messages.append(text.strip())
            self.block = ""

    def retry(self, text: str) -> None:
        # The retried request's partial text was abandoned.
        self.block = ""
        self.notes.append(text)

    def settle(self) -> None:
        if self.block.strip():
            self.messages.append(self.block.strip())
        self.block = ""

    def scrollback(self, name: str, args: tuple, kwargs: dict) -> None:
        if name in ("warning", "error") and args:
            text = str(args[0])
            self.notes.append(text)
            if text.startswith(LIMIT_PREFIX):
                self.limit = text

    def gone(self, *, stopped: bool) -> None:
        if self.ended:
            self.notes.append("The session ended, and its host stopped.")
        else:
            self.notes.append(
                "The session host stopped before this finished."
                if stopped
                else "The session host went away."
            )

    def outcome(self) -> str:
        if self.limit:
            return LIMIT
        if self.dropped or self.prompt_state == "cancelled":
            return CANCELLED
        return FAILED if self.failed else COMPLETED


# Hosts started here, reaped once they exit so none lingers as a zombie.
_children: set[asyncio.Task] = set()


def _reap(process: subprocess.Popen) -> None:
    task = asyncio.get_running_loop().create_task(asyncio.to_thread(process.wait))
    _children.add(task)
    task.add_done_callback(_children.discard)


async def start_session(
    workspace: Path,
    *,
    profile: RemoteProfile | None,
    model: str | None = None,
    resume: str | None = None,
    session_dir: Path | None = None,
    directory: Path | None = None,
    timeout: float = 300.0,
) -> HostEntry:
    """Start a host and return its entry once it accepts connections.

    `resume` continues a saved session (one whose host idled out) on the model
    it was using; otherwise `model`, else the saved default.
    """
    from pcode.preferences import load_preferences
    from pcode.sessions import read_info, resolve_session

    if resume:
        model = read_info(resolve_session(resume, session_dir, workspace)).model
    model = model or load_preferences().get("model")
    if not model:
        raise ValueError("A session host needs a model: set a default with /model.")
    identity, process, log = spawn_host(
        model=model,
        workspace=workspace,
        resume=resume,
        session_dir=session_dir,
        directory=directory,
        profile=profile,
        worktree=True,
    )
    _reap(process)
    path = socket_path(identity, directory)
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            tail = log.read_text(errors="replace").strip().splitlines()[-8:]
            raise HostError(
                f"The session host exited during startup (status {process.returncode}).\n"
                + "\n".join(tail)
                + f"\nLog: {log}"
            )
        if time.monotonic() > deadline:
            raise HostError(f"The session host did not start within {timeout:.0f}s; see {log}.")
        await asyncio.sleep(0.05)
    return find_host(identity, directory)


def changes(workspace: str | Path, base: str | None = None) -> ChangeSummary:
    """What the worktree changed since `base` (committed or not); since HEAD without one."""

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args], capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    return ChangeSummary(
        worktree=str(workspace),
        branch=git("symbolic-ref", "--quiet", "--short", "HEAD"),
        diff_stat=git("diff", "--stat", base or "HEAD"),
        untracked=git("ls-files", "--others", "--exclude-standard").splitlines(),
    )


async def send(entry: HostEntry, text: str, *, base: str | None = None) -> TurnResult:
    """Send `text` as its own turn (queued behind the host's work); wait for it to end."""
    view = CollectView()
    async with attached(entry, view) as controller:
        if controller.startup_error is not None:
            return TurnResult("", FAILED, [str(controller.startup_error)])
        await submit(controller, text, view)
        if view.dropped and controller.startup_error is not None:
            view.notes.append(str(controller.startup_error))
    view.settle()
    summary = await asyncio.to_thread(changes, entry.workspace, base)
    return TurnResult("\n\n".join(view.messages), view.outcome(), view.notes, summary)


def status(entry: HostEntry, directory: Path | None = None) -> SessionStatus:
    """What the host says about itself now; "stopped" once it is gone."""
    try:
        current = find_host(entry.id, directory)
    except LookupError:
        return SessionStatus(
            entry.id, entry.session_id, "stopped", entry.title, workspace=entry.workspace
        )
    return SessionStatus(
        host=current.id,
        session_id=current.session_id,
        state=current.state,
        title=current.title,
        last_prompt=current.last_prompt,
        turns=current.turns,
        outcome=current.outcome,
        workspace=current.workspace,
        attached=current.attached,
    )


async def stop(entry: HostEntry) -> None:
    """Cancel the running turn and everything queued behind it; the host keeps running."""
    await notify_host(entry, "cancel")
