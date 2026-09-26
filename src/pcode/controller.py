"""What runs a conversation, lifted out of the terminal UI a piece at a time.

See docs/background-sessions-plan.md. The end state is a `SessionController`
that runs in the session host while the terminal only renders; until then its
parts live here and `PreviewApp` drives them.
"""

import asyncio
from collections.abc import Callable
from typing import Protocol

# Commands that make a model request or change the toolset. Each holds the
# session busy from Enter until its handler starts, so a Ctrl+C in the same
# input batch cancels it instead of clearing the draft.
MODEL_COMMANDS = frozenset({"/compact", "/resend"})

# One queued command: the queue generation it was sent in, its text, whether
# the session was idle when it was sent, and the popup generation the terminal
# stamped on it.
Command = tuple[int, str, bool, object]

# One queued message: the queue generation it was sent in, its text, and how
# it is sent ("steering", "queue", "interrupt", "shell", "resend", "wake", and
# the "follow" modes of a turn a host started on its own).
Item = tuple[int, str, str]


class PromptQueue:
    """Messages waiting for the model, in order, mirrored into the live panel.

    The panel lists them from `activity.queued_prompts` and `queued_modes`, so
    every change goes through here and the two cannot drift. `generation`
    moves on at every `clear`: an item fetched before a clear, while a command
    or popup held the queue up, is recognised as stale and skipped. Commands
    are stamped with it too, for the same reason.
    """

    def __init__(self, activity) -> None:
        self.activity = activity
        self.generation = 0
        self._items: asyncio.Queue[Item] = asyncio.Queue()

    def __len__(self) -> int:
        return len(self.activity.queued_prompts)

    def put(self, text: str, mode: str, *, first: bool = False) -> None:
        """Queue `text`; `first` puts it ahead of everything already waiting."""
        if first:
            waiting = self._drain()
            self._items.put_nowait((self.generation, text, mode))
            for item in waiting:
                self._items.put_nowait(item)
            self.activity.queued_prompts.insert(0, text)
            self.activity.queued_modes.insert(0, mode)
        else:
            self._items.put_nowait((self.generation, text, mode))
            self.activity.queued_prompts.append(text)
            self.activity.queued_modes.append(mode)
        self._sync()

    async def get(self) -> Item:
        """The next item, stale or not; check `current` before acting on it."""
        return await self._items.get()

    def current(self, item: Item) -> bool:
        return item[0] == self.generation

    def taken(self) -> None:
        """The item just fetched is being acted on: drop it from the panel."""
        self.activity.queued_prompts.pop(0)
        self.activity.queued_modes.pop(0)
        self._sync()

    def clear(self) -> int:
        """Drop everything waiting and start a new generation. Returns how many were dropped."""
        self.generation += 1
        count = len(self.activity.queued_prompts)
        self._drain()
        self.activity.queued_prompts.clear()
        self.activity.queued_modes.clear()
        self._sync()
        return count

    def take_steering(self) -> list[str]:
        """Remove and return the current steering messages; everything else keeps its place."""
        messages = []
        for item in self._drain():
            generation, text, mode = item
            if generation == self.generation and mode == "steering":
                messages.append(text)
                index = next(
                    i
                    for i, queued in enumerate(
                        zip(self.activity.queued_prompts, self.activity.queued_modes)
                    )
                    if queued == (text, mode)
                )
                self.activity.queued_prompts.pop(index)
                self.activity.queued_modes.pop(index)
            else:
                self._items.put_nowait(item)
        self._sync()
        return messages

    def _drain(self) -> list[Item]:
        items = []
        while not self._items.empty():
            items.append(self._items.get_nowait())
        return items

    def _sync(self) -> None:
        self.activity.queued = len(self.activity.queued_prompts)


class SessionView(Protocol):
    """What the controller writes to the scrollback.

    In-process this is the terminal's `Transcript`. In a host it will be the
    socket, so arguments stay plain strings. It grows as logic moves here.
    """

    def user(self, text: str) -> None: ...
    def note(self, text: str) -> None: ...
    def warning(self, text: str) -> None: ...
    def cancelled(self) -> None: ...


def _mcp_enable(text: str) -> bool:
    return text.split()[:2] == ["/mcp", "enable"]


class SessionController:
    """Decides what a conversation does next, and stops it.

    Owns the prompt and command queues, the tasks doing work (a turn, MCP
    work, a history rewrite), and whether the session reads as busy. The
    terminal's loops still run the work; the parts of the session that have
    not moved here yet are reached through the callables passed in:

    - `cancel_policy(policy)`: what an abandoned shell wait does to its command.
    - `release_waits()`: hand a foreground shell wait back as a job handle.
    - `stop_asides()`: stop running side questions, returning how many.
    - `changed()`: the live panel's state moved; redraw it.
    """

    def __init__(
        self,
        activity,
        view: SessionView,
        *,
        cancel_policy: Callable[[str], None],
        release_waits: Callable[[], None],
        stop_asides: Callable[[], int],
        changed: Callable[[], None],
    ) -> None:
        self.activity = activity
        self.view = view
        self.cancel_policy = cancel_policy
        self.release_waits = release_waits
        self.stop_asides = stop_asides
        self.changed = changed
        self.prompts = PromptQueue(activity)
        self.commands: asyncio.Queue[Command] = asyncio.Queue()
        # Backend commands sent before the runtime was ready, in order.
        self.startup_commands: list[Command] = []
        self.ready = asyncio.Event()
        # Set while nothing holds queued prompts back: no command running, no
        # MCP work, no history rewrite. The turn loop waits on all three.
        self.command_idle = asyncio.Event()
        self.mcp_idle = asyncio.Event()
        self.compact_idle = asyncio.Event()
        for idle in (self.command_idle, self.mcp_idle, self.compact_idle):
            idle.set()
        self.live_task: asyncio.Task | None = None
        self.mcp_task: asyncio.Task | None = None
        self.compact_task: asyncio.Task | None = None
        # Commands queued but not started that hold the session busy.
        self.pending_mcp = 0
        self.pending_model_command = 0
        # The running turn is being cancelled to make way for an "interrupt"
        # message, so its failure must not clear the queue that message is in.
        self.interrupt_pending = False

    def tasks(self) -> list[asyncio.Task]:
        return [task for task in (self.live_task, self.mcp_task, self.compact_task) if task]

    def working(self) -> bool:
        """A turn, MCP work, or a history rewrite is running."""
        return any(not task.done() for task in self.tasks())

    def turn_running(self) -> bool:
        return self.live_task is not None and not self.live_task.done()

    async def idle(self) -> None:
        """Wait until nothing holds queued prompts back."""
        await self.command_idle.wait()
        await self.mcp_idle.wait()
        await self.compact_idle.wait()

    @property
    def commands_pending(self) -> bool:
        return bool(self.pending_mcp or self.pending_model_command)

    def refresh_busy(self) -> None:
        """Busy while anything is queued, pending, or running."""
        self.activity.busy = bool(
            self.activity.queued_prompts or self.commands_pending or self.working()
        )

    def submit(self, text: str, mode: str) -> None:
        """Queue a message for the model, sent the way `mode` says.

        `steering` joins the running turn at its next request, `queue` waits
        for it to end, `interrupt` cancels it, and `shell` runs a `!command`
        in turn, never as steering: its result rides the next request rather
        than being spliced into a running one.
        """
        if mode == "interrupt" and self.turn_running():
            self.clear_queue()
            self.interrupt_pending = True
            # The user is redirecting the model, not cancelling its work: an
            # in-flight shell wait is abandoned, and its command keeps running
            # under its job id.
            self.cancel_policy("detach")
            if not self.live_task.cancelling():
                self.live_task.cancel()
        self.prompts.put(text, mode)
        # Set immediately so Enter + Ctrl+C in one input batch cancels the
        # pending request rather than clearing the user's draft.
        self.activity.busy = True
        if mode == "steering" and self.turn_running():
            # Queued above, released here: the wait returns its handle and the
            # next model request carries this message.
            self.release_waits()

    def command(self, text: str, tag: object = None) -> None:
        """Queue a slash command; `tag` travels with it back to the terminal."""
        idle = not self.activity.busy and not self.activity.queued_prompts
        self.commands.put_nowait((self.prompts.generation, text, idle, tag))
        self.command_idle.clear()
        if text.split()[0] in MODEL_COMMANDS:
            self.pending_model_command += 1
            self.activity.busy = True
        if _mcp_enable(text):
            self.pending_mcp += 1
            # Enter + Ctrl+C in one input batch must cancel activation before
            # its command worker has had a chance to start OAuth.
            self.activity.busy = True

    def command_started(self, text: str) -> None:
        """A command from `command` is being handled: it no longer holds the session busy."""
        if text.split()[0] in MODEL_COMMANDS:
            self.pending_model_command -= 1
        elif _mcp_enable(text):
            self.pending_mcp -= 1
        else:
            return
        self.refresh_busy()

    def command_finished(self) -> None:
        if self.commands_pending:
            self.activity.busy = True
        if self.commands.empty() and not self.startup_commands:
            self.command_idle.set()

    def clear_queue(self) -> None:
        """Drop every queued message and pending command, saying what went."""
        self.startup_commands.clear()
        if self.commands.empty():
            self.command_idle.set()
        if self.pending_mcp:
            self.view.warning("Pending MCP enable command cancelled.")
            self.pending_mcp = 0
        if self.pending_model_command:
            self.view.warning("Pending model command cancelled.")
            self.pending_model_command = 0
        if count := self.prompts.clear():
            self.view.note(f"Cleared {count} queued message(s).")

    def cancel(self) -> None:
        """Ctrl+C: clear the queue and stop what is running."""
        self.interrupt_pending = False
        self.clear_queue()
        active = [task for task in self.tasks() if not task.done()]
        # Ctrl+C means "stop working", so a command the turn is waiting on is
        # stopped with it. A typed follow-up takes the other branch and only
        # abandons the wait. Either way a job the model explicitly backgrounded
        # keeps running: nothing is waiting on it to abandon.
        self.cancel_policy("stop")
        if active:
            # Repeated interrupts must not interrupt persistence/auth cleanup.
            # Side questions are deliberately parallel: an interrupt aimed at
            # the turn must not also throw away work the turn is not doing.
            for task in active:
                if not task.cancelling():
                    task.cancel()
        elif stopped := self.stop_asides():
            self.view.note(f"Stopped {stopped} side question(s).")
        else:
            self.activity.busy = False
            self.view.cancelled()

    def take_steering(self) -> list[str]:
        """The runtime's hook: steering messages for the next model request."""
        messages = self.prompts.take_steering()
        for text in messages:
            self.activity.start_prompt(text)
            self.view.user(text)
        if messages:
            self.changed()
        return messages

    def turn_ended(self, success: bool) -> None:
        """Settle the queue after a turn: a failure drops what was waiting behind it."""
        if not success and not self.interrupt_pending:
            self.clear_queue()
        self.interrupt_pending = False
        self.refresh_busy()
