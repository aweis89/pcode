"""`pcode --attach HOST --print PROMPT`: one message or command for a running host, no editor.

It attaches like a terminal, sends the message (queued behind whatever the host
is already doing) or the slash command, writes what that produces the way a
local `--print` writes its turn (the reply on stdout, the rest on stderr), and
detaches. The host keeps running, and every other terminal sees the turn too.

Only this caller's own turn or command is written: a turn another terminal
started, or one already running when this attached, is not.
"""

import asyncio
import sys

from pcode.controller import TERMINAL_COMMANDS
from pcode.host_protocol import HostEntry
from pcode.remote import HostError, RemoteController, stop_entry
from pcode.rpc import RemoteError
from pcode.stream_display import PrintedReply
from pcode.ui import Activity


class PrintView:
    """The view a `--print` attach gives the host: its own turn or command, nothing else."""

    def __init__(self, transcript, reply: PrintedReply) -> None:
        self.transcript = transcript
        self.reply = reply
        # The host's live-panel fields, mirrored by the RemoteController.
        self.activity = Activity()
        # The message, once the host has queued it; how many identical ones
        # were queued ahead of it (their turns start first); whether its turn
        # is running; whether the command sent is running.
        self.prompt: str | None = None
        self.ahead = 0
        self.turn = False
        self.command = False
        self.failed = False
        # The message left the host's queue without running.
        self.dropped = False
        self.finished = asyncio.Event()

    @property
    def showing(self) -> bool:
        return self.turn or self.command

    def queued(self, prompt: str, ahead: int) -> None:
        """The host queued the message: its turn is the next with this text after `ahead`."""
        self.prompt, self.ahead = prompt, ahead
        self.redraw()

    def host_gone(self, *, stopped: bool) -> None:
        if self.finished.is_set():
            return
        self.reply.settle()
        if stopped and self.command and not self.turn:
            # Most likely this command ending the session (`/worktree finish`).
            self.transcript.note("The session host stopped.")
        else:
            self.transcript.error(
                "The session host stopped." if stopped else "The session host went away."
            )
            self.failed = True
        self.finished.set()

    def redraw(self) -> None:
        """The host's state moved: notice the message leaving its queue without running."""
        if self.prompt is None or self.turn or self.finished.is_set():
            return
        if not self.activity.busy:
            # A queued message holds the host busy until its turn has started,
            # so an idle host means a Ctrl+C or a failed turn cleared the queue.
            self.dropped = self.failed = True
            self.finished.set()

    # The turn

    def turn_started(self, text: str, *, echo: bool) -> None:
        if text != self.prompt or self.turn or self.finished.is_set():
            return
        if self.ahead:
            self.ahead -= 1  # An identical message queued before this one.
        else:
            self.turn = True

    def turn_event(self, event) -> None:
        if self.turn:
            self.reply.event(event)

    def turn_retry(self, text: str) -> None:
        if self.turn:
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

    # Scrollback, written only while this caller's own work runs

    def _scrollback(name: str):
        def write(self, *args, **kwargs) -> None:
            if self.showing:
                self.reply.settle()
                getattr(self.transcript, name)(*args, **kwargs)

        write.__name__ = name
        return write

    user = _scrollback("user")
    note = _scrollback("note")
    retained_note = _scrollback("retained_note")
    flash = _scrollback("flash")
    cancelled = _scrollback("cancelled")
    tool_result = _scrollback("tool_result")
    shell_result = _scrollback("shell_result")
    del _scrollback

    def warning(self, text: str) -> None:
        if self.showing:
            self.reply.settle()
            self.transcript.warning(text)
            # A command's warning is a refusal ("unavailable while working"),
            # which a script must not read as success. A turn's is only advice.
            self.failed = self.failed or self.command

    def error(self, text: str, *, title: str = "Error") -> None:
        if self.showing:
            self.reply.settle()
            self.transcript.error(text, title=title)
            self.failed = True

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
        """Anything else only repaints a live panel this caller does not have."""
        if name.startswith("_"):
            raise AttributeError(name)
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
        # The host answers with `queued` first, which arms the view.
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
