"""`pcode --attach HOST --print PROMPT`: one message or command for a running host, no editor.

It attaches like a terminal, sends the message (queued behind whatever the host
is already doing) or the slash command, writes what that produces the way a
local `--print` writes its turn (the reply on stdout, the rest on stderr), and
detaches. The host keeps running, and every other terminal sees the turn too.

The host tells this caller alone when its message's turn starts, or that it
was dropped, in order with everything else it shows (`pcode.host._Owner`), so
a turn another terminal started, or one already running, is never mistaken
for it. Notes other terminals cause while it runs are written all the same.
"""

import asyncio
import sys

from pcode.controller import TERMINAL_COMMANDS
from pcode.host_protocol import HostEntry
from pcode.remote import HostError, RemoteController, stop_entry
from pcode.rpc import RemoteError
from pcode.runtime import PlanUpdated
from pcode.stream_display import PrintedReply
from pcode.ui import Activity


class PrintView:
    """The view a `--print` attach gives the host: its own turn or command, nothing else."""

    # Scrollback written as is, while this caller's own turn or command runs.
    SCROLLBACK = frozenset(
        {"user", "note", "retained_note", "flash", "cancelled", "tool_result", "shell_result"}
    )

    def __init__(self, transcript, reply: PrintedReply) -> None:
        self.transcript = transcript
        self.reply = reply
        # The host's live-panel fields, mirrored by the RemoteController.
        self.activity = Activity()
        # Busy while the host works, on this message or the turns ahead of it.
        self.tab = reply.tab_progress(self.activity)
        # Whether the message's turn is running, or the command sent is.
        self.turn = False
        self.command = False
        self.failed = False
        # The message was dropped from the host's queue without running.
        self.dropped = False
        # The command sent ended the session, so the host stopping is its doing.
        self.ended = False
        self.finished = asyncio.Event()

    @property
    def showing(self) -> bool:
        return self.turn or self.command

    # What the host says about this caller's own message or command

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
        self.reply.settle()
        if self.ended:
            self.transcript.note("The session ended, and its host stopped.")
        else:
            self.transcript.error(
                "The session host stopped before this finished."
                if stopped
                else "The session host went away."
            )
            self.failed = True
        self.finished.set()

    # The turn

    def turn_event(self, event) -> None:
        if self.turn:
            self.tab.turn_event()
            if isinstance(event, PlanUpdated):
                self.activity.plan = event.items
            self.reply.event(event)

    def turn_retry(self, text: str) -> None:
        if self.turn:
            self.tab.turn_retry()
            self.reply.settle()
            self.transcript.note(text)

    def turn_ended(self) -> None:
        if self.turn:
            self.reply.settle()

    async def after_turn(self) -> None:
        if self.turn:
            self.turn = False
            self.failed = self.failed or self.activity.prompt_state != "done"
            self.finished.set()

    # Scrollback

    def _scrollback(self, name: str, *args, **kwargs) -> None:
        if self.showing:
            self.reply.settle()
            getattr(self.transcript, name)(*args, **kwargs)

    def warning(self, text: str) -> None:
        self._scrollback("warning", text)
        # A command's warning is a refusal ("unavailable while working"), which
        # a script must not read as success. A turn's is advice; how the turn
        # ended says whether it worked.
        self.failed = self.failed or self.command

    def error(self, text: str, *, title: str = "Error") -> None:
        self._scrollback("error", text, title=title)
        self.failed = self.failed or self.command

    def show_events(self, events) -> None:
        if self.showing:
            self.transcript.events(tuple(events))

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


async def print_to_host(entry: HostEntry, prompt: str, *, transcript, present, stdout=None) -> bool:
    """Send `prompt` to the host at `entry`, write what it produces; returns whether it worked."""
    prompt = prompt.strip()
    name = prompt.split(maxsplit=1)[0] if prompt.startswith("/") else ""
    if name == "/stop":
        return await _stop(entry, prompt, transcript)
    if name in TERMINAL_COMMANDS:
        transcript.error(f"{name} needs the interactive terminal: pcode --attach {entry.id}")
        return False
    reply = PrintedReply(
        sys.stdout if stdout is None else stdout, transcript=transcript, present=present
    )
    view = PrintView(transcript, reply)
    async with view.tab.shown():
        return await _send(entry, prompt, name, view)


async def _send(entry: HostEntry, prompt: str, name: str, view: PrintView) -> bool:
    """Attach, send the message or command, and write what it produces."""
    transcript, reply = view.transcript, view.reply
    controller, welcome = await RemoteController.connect(entry.socket, view, view.activity)
    # Wired before anything else is awaited, so no close goes unnoticed.
    controller.on_closed = lambda: view.host_gone(stopped=controller.host_stopped)
    if controller.peer.closed.is_set():
        controller.on_closed()
    try:
        # What the host showed before this attached is not this caller's.
        await controller.start({**welcome, "calls": []})
        if controller.startup_error is not None:
            transcript.error(str(controller.startup_error), title="Agent startup failed")
            return False
        if name:
            view.command = True
            ran = await _host_call(controller, "run", prompt, view)
            view.command = False
            if ran is False and not view.failed:
                transcript.error(f"{name} did not run: the host's queue was cleared first.")
                return False
            # A host that went away has said whether that was a failure.
            return not view.failed
        await _host_call(controller, "send", prompt, view)
        try:
            await view.finished.wait()
        except asyncio.CancelledError:
            # Ctrl+C only detaches: the host's own cancel would also clear
            # every other terminal's queue. Attach to stop the turn there.
            reply.settle()
            where = "keeps running" if view.turn else "is still queued"
            transcript.note(f"Detached; the message {where} in host {entry.id}.")
            raise
        if view.dropped:
            # Startup can fail after this attached, and then drops the queue.
            error = controller.startup_error
            transcript.error(
                str(error) if error else "The host dropped this message before it ran.",
                title="Agent startup failed" if error else "Error",
            )
        return not view.failed
    finally:
        await controller.detach()


async def _host_call(controller: RemoteController, method: str, prompt: str, view: PrintView):
    """A call only newer hosts have; None when the host went away (and has said so)."""
    try:
        return await controller.peer.request(method, prompt)
    except RemoteError as error:
        if error.type_name == "PermissionError":
            raise HostError(
                "The session host runs older pcode; /restart it to send it messages headlessly."
            ) from error
        raise
    except ConnectionError:
        view.host_gone(stopped=controller.host_stopped)
        return None


async def _stop(entry: HostEntry, prompt: str, transcript) -> bool:
    """`/stop`: end the host; it tidies its own worktree, as nobody is here to ask."""
    if prompt != "/stop":
        transcript.error("Usage: /stop")
        return False
    await stop_entry(entry)
    line = f"Stopped session host {entry.id}."
    if entry.session_id:
        line += f" Continue with: pcode --continue {entry.session_id}"
    transcript.note(line)
    return True
