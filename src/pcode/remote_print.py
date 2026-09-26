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
        # The message sent, once it is; whether the host's queue has shown it;
        # whether its turn is running; whether the command sent is running.
        self.prompt: str | None = None
        self.queued = False
        self.turn = False
        self.command = False
        self.failed = False
        self.finished = asyncio.Event()

    @property
    def showing(self) -> bool:
        return self.turn or self.command

    def finish(self, ok: bool) -> None:
        self.failed = self.failed or not ok
        self.finished.set()

    def host_gone(self) -> None:
        if not self.finished.is_set():
            self.reply.settle()
            self.transcript.error("The session host went away.")
            self.finish(False)

    def redraw(self) -> None:
        """The host's queue moved: notice the message leaving it without having run."""
        if self.prompt is None or self.turn or self.finished.is_set():
            return
        if self.prompt in self.activity.queued_prompts:
            self.queued = True
        elif self.queued and not self.activity.busy:
            # Taking it to run keeps the host busy until its turn has started,
            # so an idle host without it means a Ctrl+C or failure cleared it.
            self.transcript.error("The host dropped this message from its queue before it ran.")
            self.finish(False)

    # The turn

    def turn_started(self, text: str, *, echo: bool) -> None:
        if text == self.prompt and not self.turn and not self.finished.is_set():
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
            self.finish(self.activity.prompt_state == "done")

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
    try:
        # What the host showed before this attached is not this caller's.
        await controller.start({**welcome, "calls": []})
        controller.on_closed = view.host_gone
        if controller.startup_error is not None:
            transcript.error(str(controller.startup_error), title="Agent startup failed")
            return False
        if name:
            view.command = True
            try:
                await controller.peer.request("run", prompt)
            except RemoteError as error:
                # A host from before `run` refuses it by name.
                raise HostError(
                    f"The session host runs older pcode ({error}); restart it to send it commands."
                ) from error
            except ConnectionError:
                view.host_gone()
            view.command = False
            return not view.failed
        view.prompt = prompt
        controller.submit(prompt, "queue")
        try:
            await view.finished.wait()
        except asyncio.CancelledError:
            # Ctrl+C stops this caller's own turn. One still queued stays: the
            # host's cancel would also stop whatever another terminal is running.
            if view.turn:
                controller.cancel()
            else:
                transcript.note(f"Detached; the message is still queued in host {entry.id}.")
            raise
        return not view.failed
    finally:
        await controller.detach()


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
