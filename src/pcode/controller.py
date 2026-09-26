"""What runs a conversation, lifted out of the terminal UI a piece at a time.

See docs/background-sessions-plan.md. The end state is a `SessionController`
that runs in the session host while the terminal only renders; until then its
parts live here and `PreviewApp` drives them.
"""

import asyncio
import inspect
from collections.abc import Callable
from contextlib import aclosing
from typing import Protocol

from pcode.commands import Command, CommandRegistry
from pcode.jobs import OUTPUT_TAIL_BYTES, WATCHED_PREFIX, format_duration
from pcode.preferences import SETTINGS, load_preferences
from pcode.runtime import CommandOutput, JobFinished, ToolSummary
from pcode.shell_mode import execute, shell_command
from pcode.tool_display import command_text

# What works while the conversation runs in a session host. Everything else
# reaches into a runtime this process does not have, so it is refused rather
# than acting on nothing. Skills are prompts and always work.
HOSTED_COMMANDS = {
    "/help",
    "/config",
    "/quit",
    "/status",
    "/diffs",
    "/switch",
    "/stop",
    "/restart",
    "/resume",
    "/show-tasks",
    "/autohide-tasks",
    "/show-thinking",
    "/show-edits",
    "/show-commands",
    "/theme",
    "/syntax",
    "/theme-preview",
    "/redraw",
}

# Commands that make a model request or change the toolset. Each holds the
# session busy from Enter until its handler starts, so a Ctrl+C in the same
# input batch cancels it instead of clearing the draft.
MODEL_COMMANDS = frozenset({"/compact", "/resend"})

# Commands that only change the display. They run even before the runtime is
# ready and are never dropped by a Ctrl+C clearing the queue.
FRONTEND_COMMANDS = frozenset(
    {
        "/quit",
        "/exit",
        "/help",
        "/commands",
        "/theme",
        "/theme-preview",
        "/syntax",
        "/show-tasks",
        "/autohide-tasks",
        "/show-thinking",
        "/show-edits",
        "/show-commands",
        "/redraw",
        "/config",
    }
)

# One queued command: the queue generation it was sent in, its text, whether
# the session was idle when it was sent, and the popup generation the terminal
# stamped on it.
QueuedCommand = tuple[int, str, bool, object]

# Modes of a turn a session host started on its own, which a terminal shows
# rather than sends: "follow-quiet" leaves the prompt out of scrollback.
FOLLOW_MODES = ("follow", "follow-quiet")

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
    """What the controller shows, implemented by whatever is displaying the session.

    In-process that is `PreviewApp`; in a session host it will be the socket,
    so arguments stay plain values and runtime events. It grows as logic moves
    here from the terminal.
    """

    # Scrollback
    def user(self, text: str) -> None: ...
    def note(self, text: str) -> None: ...
    def warning(self, text: str) -> None: ...
    def error(self, text: str, *, title: str = "Error") -> None: ...
    def cancelled(self) -> None: ...
    def tool_result(self, event) -> None: ...
    def shell_result(
        self, command: str, output: str, *, failed: bool, elapsed_seconds: float
    ) -> None: ...

    # The live panel's own state (queue, busy, prompt row) changed; repaint.
    def redraw(self) -> None: ...

    # A turn, in order: started, its events, retries, ended. `drop_output`
    # removes a live command preview once its command is done.
    def turn_started(self, text: str, *, echo: bool) -> None: ...
    def turn_event(self, event) -> None: ...
    def turn_retry(self, text: str) -> None: ...
    def turn_ended(self) -> None: ...
    def finish_text(self) -> None: ...
    def show_output(self, event) -> None: ...
    def drop_output(self, call_id: str) -> None: ...
    async def after_turn(self) -> None: ...

    # A command the controller does not handle itself, run by the terminal
    # that sent it; `after_command` follows every command.
    async def run_command(self, text: str, *, idle: bool, tag: object) -> None: ...
    async def after_command(self) -> None: ...

    # Popups the session's commands open, in the terminal that sent them.
    async def browse_jobs(self) -> None: ...


def _mcp_enable(text: str) -> bool:
    return text.split()[:2] == ["/mcp", "enable"]


def wake_row(text: str) -> tuple[str, str]:
    """The live row for a turn a finished job started: a badge, not an echo."""
    return "Job finished", text.partition("\n")[0].partition(" Read ")[0]


def delivered_job(event) -> str | None:
    """The job a top-level `wait_for_job` or `job_output` call just collected, if any."""
    if (
        isinstance(event, ToolSummary)
        and event.name in {"wait_for_job", "job_output"}
        and not event.parent_call_id
    ):
        return event.detail.partition(" ")[0]
    return None


class SessionController:
    """Runs a conversation: what it does next, the turns themselves, and stopping them.

    Owns the runtime, the prompt and command queues, the tasks doing work (a
    turn, MCP work, a history rewrite), and whether the session reads as busy.
    Everything it shows goes through `view`; `activity` holds the live panel's
    session state (busy, queue, prompt row), which the view paints.

    `app` is the terminal, for the parts of the session that have not moved
    here yet (docs/background-sessions-plan.md tracks them). Each slice of the
    refactor removes uses of it; the host can run a controller once none remain.
    """

    def __init__(self, app, view: SessionView, activity, runtime=None) -> None:
        self.app = app
        self.view = view
        self.activity = activity
        self.runtime = runtime
        # Whether the owner is shutting down, so a cancelled turn is not a Ctrl+C.
        self.closing: Callable[[], bool] = lambda: False
        self.prompts = PromptQueue(activity)
        self.commands: asyncio.Queue[QueuedCommand] = asyncio.Queue()
        # Backend commands sent before the runtime was ready, in order.
        self.startup_commands: list[QueuedCommand] = []
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
        # The MCP server (or "defaults") being enabled or signed out of.
        self.mcp_enabling: str | None = None
        # The watched job's output tail, as last pinned in the live panel.
        self.pinned: CommandOutput | None = None
        # The slash commands the session handles; the terminal runs the rest.
        self.registry = CommandRegistry()
        self.register_commands()

    def register_commands(self) -> None:
        for command in (
            Command(
                "/jobs",
                "Browse shell jobs and their output; stop ID / stop all / watch ID / unwatch",
                self.jobs,
                free_arguments=True,
                argument_provider=self.jobs_arguments,
                group="Session",
            ),
        ):
            self.registry.register(command)

    @property
    def hosted(self) -> bool:
        # `is True`: a Mock runtime in tests answers every attribute.
        return getattr(self.runtime, "remote", False) is True

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

    # Shell waits

    def set_cancel_policy(self, policy: str) -> None:
        """Say what an abandoned shell wait should do to its command.

        Set before cancelling, because by the time the tool call sees
        `CancelledError` there is nothing left to tell it apart from any other
        cancellation. Reset to the safe default once the turn is over.
        """
        registry = getattr(self.runtime, "jobs", None)
        if registry is not None:
            registry.cancel_policy = policy
        elif self.hosted:
            # Sent with the cancel, for the host's own registry.
            self.runtime.cancel_policy = policy

    def release_shell_waits(self) -> None:
        """Hand any foreground shell wait back to the model as a job handle.

        Steering is delivered at the next model request, and a wait on a slow
        command is what stands between now and that request. Ending the wait
        gets the message there promptly; the command itself is untouched.
        """
        registry = getattr(self.runtime, "jobs", None)
        if registry is not None:
            registry.release_waits()
        elif self.hosted:
            # The host pulls steering at its next request like a local runtime
            # does, but it can only take what has been sent to it: send it now.
            self.runtime.release_waits()

    # Jobs

    def report_finished_jobs(self, job_id: str | None = None) -> list:
        """Announce job exits in scrollback, each one once. Returns those announced."""
        registry = getattr(self.runtime, "jobs", None)
        if registry is None:
            return []
        from pcode.shell import REDUCED_SHELL_OUTPUT, result_projection

        finished = registry.take_announcements("ui", job_id)
        for job in finished:
            output, truncated = registry.read_output(job)
            if truncated:
                output = REDUCED_SHELL_OUTPUT + "\n" + output
            result = output + f"\n[{job.id} · {job.outcome()} · {format_duration(job.elapsed)}]"
            event = JobFinished(
                "shell",
                f"{command_text(job.label())} → {job.id} · {job.outcome()}",
                failed=job.stopped or job.exit_code != 0,
                elapsed_seconds=job.elapsed,
                command=command_text(job.command),
                result=command_text(result_projection(result)),
                purpose=command_text(job.purpose),
            )
            saved = getattr(self.runtime, "session", None)
            if saved is not None:
                saved.event(event, run_id=saved.tree.active or "")
            self.view.tool_result(event)
        return finished

    def report_delivered_job(self, job_id: str) -> None:
        """Write a job's exit where the call that collected it settled.

        The call's own row is left out of scrollback because the exit notice
        says the same thing. Holding that notice until idle would print it
        after the final answer, long after the model acted on the result.
        A job that is still running, or was already reported, prints nothing.
        """
        registry = getattr(self.runtime, "jobs", None)
        job = registry.get(job_id) if registry is not None else None
        if job is None or job.running or "ui" in job.announced:
            return
        # A suppressed row does not commit streamed prose; the notice must not
        # land ahead of text the model wrote before making the call.
        self.view.finish_text()
        self.report_finished_jobs(job_id)

    def wake_prompt(self) -> str | None:
        """The turn a finished job starts on its own, or None when nothing should.

        Only a job the model launched and expects to hear about wakes it: one
        it backgrounded, or was handed a handle for when a wait ended early.
        An adopted job belongs to a model that is gone. The text is exactly
        the notice the model would have received at its next request, so
        waking costs a request, never a different conversation. Called while
        idle: a job that ended mid-turn after the last request is included,
        because nothing else is going to deliver it.
        """
        registry = getattr(self.runtime, "jobs", None)
        if registry is None or not self.app.model:
            return None
        wakeable = [
            job
            for job in registry.jobs.values()
            if not job.running
            # A stop is the user's or the model's own doing, not news to act on.
            and not job.stopped
            and not job.adopted
            and "model" not in job.announced
            and registry.announceable(job)
        ]
        if not wakeable:
            return None
        if load_preferences().get("job_wake", SETTINGS["job_wake"].default) != "on":
            return None
        from pcode.job_notices import notice_for

        for job in wakeable:
            job.announced.add("model")
        return "\n\n".join(notice_for(registry, job) for job in wakeable)

    # Sending

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
            self.set_cancel_policy("detach")
            if not self.live_task.cancelling():
                self.live_task.cancel()
        self.prompts.put(text, mode)
        # Set immediately so Enter + Ctrl+C in one input batch cancels the
        # pending request rather than clearing the user's draft.
        self.activity.busy = True
        if mode == "steering" and self.turn_running():
            # Queued above, released here: the wait returns its handle and the
            # next model request carries this message.
            self.release_shell_waits()

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

    # Stopping

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
        self.set_cancel_policy("stop")
        if active:
            # Repeated interrupts must not interrupt persistence/auth cleanup.
            # Side questions are deliberately parallel: an interrupt aimed at
            # the turn must not also throw away work the turn is not doing.
            for task in active:
                if not task.cancelling():
                    task.cancel()
        elif stopped := self.app.asides.cancel():
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
            self.view.redraw()
        return messages

    def turn_ended(self, success: bool) -> None:
        """Settle the queue after a turn: a failure drops what was waiting behind it."""
        if not success and not self.interrupt_pending:
            self.clear_queue()
        self.interrupt_pending = False
        self.refresh_busy()

    async def consume_commands(self) -> None:
        """Run slash commands one at a time, in the order they were sent."""
        app = self.app
        while app.running:
            generation, text, submitted_idle, tag = await self.commands.get()
            name = text.split(maxsplit=1)[0]
            try:
                if name not in FRONTEND_COMMANDS:
                    if not self.ready.is_set():
                        # Keep consuming frontend-only commands while backend
                        # commands wait, preserving their order for readiness.
                        self.startup_commands.append((generation, text, submitted_idle, tag))
                        continue
                    if generation != self.prompts.generation:
                        continue
                    if app._startup_error is not None:
                        self.view.warning("Agent startup failed; restart pcode to retry.")
                        continue
                self.command_started(text)
                if self.registry.find(name) is None:
                    await self.view.run_command(text, idle=submitted_idle, tag=tag)
                else:
                    await self.run_command(text)
            except Exception as error:
                app.command_failed(name, error)
            finally:
                self.command_finished()
            await self.view.after_command()

    async def run_command(self, text: str) -> None:
        """Run one of the session's own commands; a handler may be sync or async."""
        parts = text.strip().split(maxsplit=1)
        command = self.registry.find(parts[0])
        if self.hosted and command.name not in HOSTED_COMMANDS:
            self.view.error(
                f"{command.name} is not available yet for a session running in a "
                "session host. /switch, /resume, /stop, /status, and display commands are."
            )
            return
        argument = parts[1].strip() if len(parts) > 1 else ""
        try:
            if argument and not command.free_arguments and argument not in command.arguments:
                usage = "|".join(command.arguments)
                raise ValueError(f"Usage: {command.name}" + (f" [{usage}]" if usage else ""))
            result = command.handler(argument)
            if inspect.isawaitable(result):
                await result
        except ValueError as error:
            self.view.error(str(error))

    # Turns

    async def consume(self) -> None:
        """Run queued messages one at a time, each once nothing holds the queue."""
        from pcode.live import error_message

        app = self.app
        await self.ready.wait()
        while app.running:
            await self.idle()
            item = await self.prompts.get()
            _generation, text, mode = item
            if app._startup_error is not None:
                self.clear_queue()
                self.activity.busy = False
                self.view.warning("Agent startup failed; restart pcode to retry.")
                continue
            await self.idle()
            if not app.running:
                return
            if not self.prompts.current(item):
                continue  # Cancelled while waiting for a command/modal.
            self.prompts.taken()
            # A model chosen mid-run takes effect here, before the request
            # that follows it is sent.
            if app.pending_model is not None:
                await app.apply_pending_model()
            success = True
            try:
                resend = mode == "resend"
                if mode == "shell":
                    self.live_task = asyncio.create_task(self.run_shell(text))
                    try:
                        success = await self.live_task
                    except asyncio.CancelledError:
                        if self.closing():
                            return
                        success = False
                elif resend or mode in FOLLOW_MODES or app.handle(text):
                    wake = mode == "wake"
                    if wake:
                        label, detail = wake_row(text)
                        self.activity.start_prompt(label, kind="system", detail=detail)
                    else:
                        self.activity.start_prompt(text)
                    self.runtime.take_steering = self.take_steering
                    self.live_task = asyncio.create_task(
                        self.run_turn(
                            text,
                            resend=resend,
                            wake=wake,
                            follow=mode in FOLLOW_MODES,
                            echo=mode != "follow-quiet",
                        )
                    )
                    try:
                        success = await self.live_task
                    except asyncio.CancelledError:
                        # Cancellation before run_turn's first instruction.
                        if self.closing():
                            return
                        success = False
                        self.activity.finish_prompt("cancelled")
                        self.view.cancelled()
            except Exception as error:
                self.view.error(error_message(error), title="Agent failed")
                success = False
            finally:
                self.live_task = None
            if self.closing():
                return
            self.turn_ended(success)
            # Adopt it as soon as the turn ends so the footer and /status
            # agree with what the next request will use.
            if app.pending_model is not None:
                await app.apply_pending_model()
            await self.view.after_turn()

    async def run_turn(
        self,
        text: str,
        *,
        resend: bool = False,
        wake: bool = False,
        follow: bool = False,
        echo: bool = True,
    ) -> bool:
        """Run a turn, or with `follow` show one a session host started on its own.

        `echo` False leaves the prompt out of scrollback: steering this terminal
        forwarded to a host is already there.
        """
        from pcode.live import error_message

        runtime = self.runtime
        if follow and getattr(runtime, "pending_turn", None) is None:
            # Already shown: a prompt sent from here caught up with it first.
            self.activity.finish_prompt("done")
            return True
        self.view.turn_started(text, echo=echo and not wake)
        if wake:
            # Scrollback already carries the job's summary line; the prompt is
            # pcode's, so it is labelled as system work rather than quoted.
            label, detail = wake_row(text)
            self.activity.start_prompt(label, kind="system", detail=detail)
        else:
            self.activity.start_prompt(text)
        self.activity.status = "Waiting for model…"

        def compaction_notice(text):
            self.activity.status = text
            self.view.note(text)

        runtime.compaction_notice = compaction_notice

        def retry_notice(text):
            # Separate abandoned partial text/thinking from the next attempt.
            self.activity.status = text
            self.view.turn_retry(text)

        if hasattr(runtime, "retry_notice"):
            runtime.retry_notice = retry_notice
        if hasattr(runtime, "warning_notice"):
            runtime.warning_notice = self.view.warning
        failure = None
        cancelled = False
        source = runtime.follow() if follow else runtime.stream(None if resend else text)
        try:
            async with aclosing(source) as stream:
                async for event in stream:
                    self.view.turn_event(event)
                    if (job_id := delivered_job(event)) is not None:
                        self.report_delivered_job(job_id)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as error:
            # Another terminal cancelled the host's turn this one was showing.
            cancelled = type(error).__name__ == "HostTurnCancelled"
            failure = None if cancelled else error
        finally:
            self.view.turn_ended()
            self.activity.status = ""
        if cancelled and getattr(runtime, "detaching", False) is True:
            # Switched away: the turn carries on in its host, unannounced here.
            self.activity.finish_prompt("done")
            return False
        # Abandoning a wait is the exception, not the rule: restore the safe
        # default so the next Ctrl+C-free cancellation cannot kill a command.
        self.set_cancel_policy("detach")
        self.activity.finish_prompt("cancelled" if cancelled else "failed" if failure else "done")
        self.report_finished_jobs()
        self.view.redraw()
        if cancelled:
            self.view.cancelled()
            # A cancelled turn used to take its commands with it. Say plainly
            # what survived, so "still running" is never a surprise.
            registry = getattr(runtime, "jobs", None)
            running = registry.running() if registry is not None else []
            if running:
                self.view.note(
                    f"{len(running)} command(s) still running: "
                    + ", ".join(f"[{job.id}] {job.label()}" for job in running[:3])
                    + ". Use /jobs to list or stop them."
                )
        elif failure:
            from pcode.diagnostics import stale_install

            self.view.error(error_message(failure), title="Agent failed")
            if hint := stale_install():
                self.view.warning(hint)
        if (cancelled or failure) and runtime.session:
            directory = runtime.session.directory
            # Name the traceback file rather than the directory it sits in: the
            # frames are the point of looking, and a cancelled turn writes none.
            errors = directory / "errors.log"
            target = errors if failure and errors.exists() else directory
            self.view.note(f"Session and diagnostics: {target}")
            if runtime.recovery_blocked:
                self.view.warning(runtime.recovery_blocked)
        return not (cancelled or failure)

    async def run_shell(self, text: str) -> bool:
        """Run a `!command` the user typed and hand its result to the runtime.

        Output streams into the live command panel while it runs and is
        mirrored to scrollback when it ends. The model sees it on the next
        prompt, as a `shell` tool call, so ask a follow-up to discuss it.
        """
        # Lazy: pcode.shell pulls in the agent stack, which startup avoids.
        from pcode.shell import preview_text

        command = shell_command(text)
        assert command is not None
        call_id = f"shell_mode_{id(self):x}"
        cwd, env = (
            self.runtime.shell_environment()
            if hasattr(self.runtime, "shell_environment")
            else (self.app.workspace, None)
        )
        self.view.user(text)
        self.activity.start_prompt(text)
        self.activity.user_command = True
        self.activity.status = "Running command…"
        buffered = ""

        def show(chunk: str) -> None:
            nonlocal buffered
            buffered += chunk
            self.view.turn_event(CommandOutput(call_id, command, preview_text(buffered)))

        run = None
        try:
            run = await execute(command, cwd=cwd, env=env, on_output=show)
        except asyncio.CancelledError:
            pass
        except OSError as error:
            self.view.error(str(error), title="Command failed to start")
        finally:
            self.view.drop_output(call_id)
            self.activity.user_command = False
            self.activity.status = ""
        if run is None:
            self.activity.finish_prompt("cancelled")
            self.view.warning("Command cancelled; the model was not told about it.")
            self.view.redraw()
            return False
        self.view.shell_result(
            command, run.output, failed=run.failed, elapsed_seconds=run.elapsed_seconds
        )
        if hasattr(self.runtime, "record_shell"):
            visible = await self.runtime.record_shell(run)
            reduced = not isinstance(visible, str) or visible != run.tool_result()
            self.view.note(
                "The model sees this command and its "
                + ("reduced output" if reduced else "output")
                + " with your next message."
            )
        self.activity.finish_prompt("done")
        self.view.redraw()
        return True

    # Work beside the turn loop that holds queued prompts back

    def start_mcp_task(self, name, coroutine, *, status: str, cancelled: str) -> None:
        """Run MCP work outside the model loop; queued prompts wait for it."""
        self.mcp_idle.clear()
        self.mcp_enabling = name
        self.activity.busy = True
        self.activity.status = status

        def finished(task):
            success = False
            try:
                task.result()
                success = True
            except asyncio.CancelledError:
                self.view.warning(cancelled)
            except Exception as error:
                self.app.report_mcp_error(name, error)
            finally:
                if not success:
                    self.clear_queue()
                self.mcp_task = None
                self.refresh_busy()
                self.activity.status = ""
                self.mcp_enabling = None
                self.mcp_idle.set()
                self.view.redraw()

        self.mcp_task = asyncio.create_task(coroutine)
        # A done callback also handles cancellation before the coroutine starts.
        self.mcp_task.add_done_callback(finished)

    def start_mcp_enable(self, name: str) -> None:
        self.view.note(
            f"Enabling MCP '{name}'. OAuth sign-in happens now if needed; "
            "Ctrl+C cancels. No model request is made."
        )
        self.start_mcp_task(
            name,
            self.app.enable_mcp(name),
            status=f"Enabling MCP '{name}' — complete browser sign-in if prompted…",
            cancelled=f"MCP '{name}' sign-in cancelled; server remains off.",
        )

    def start_skill_mcp(self, skill: str, names) -> None:
        """Enable what `skill` declares before its prompt, which waits on MCP work."""
        from pcode.mcp import config_path, configured_servers

        state = getattr(self.runtime, "mcp", None)
        if state is None:
            return
        try:
            configured = configured_servers()
        except ValueError as error:
            self.view.error(str(error))
            return
        if unknown := [name for name in names if name not in configured]:
            self.view.warning(
                f"The {skill} skill asks for MCP {', '.join(unknown)}, "
                f"not configured in {config_path()}."
            )
        wanted = [name for name in names if name in configured and name not in state.enabled]
        if not wanted:
            return
        # Same rule as /mcp enable: never swap toolsets under a running turn.
        if self.mcp_task is not None or self.turn_running():
            listed = " ".join(f"`/mcp enable {name}`" for name in wanted)
            self.view.warning(
                f"The {skill} skill asks for MCP {', '.join(wanted)}, which cannot be "
                f"enabled while working. Run {listed} after this turn."
            )
            return
        self.view.note(
            f"Enabling MCP {', '.join(wanted)} for the {skill} skill. "
            "OAuth sign-in happens now if needed; Ctrl+C cancels."
        )
        self.start_mcp_task(
            ", ".join(wanted),
            self.app.enable_skill_mcp(skill, wanted),
            status=f"Enabling MCP for the {skill} skill — complete sign-in if prompted…",
            cancelled=f"MCP sign-in for the {skill} skill cancelled; its prompt was not sent.",
        )

    def start_mcp_defaults(self) -> None:
        from pcode.mcp import default_servers

        self.app.mcp_defaults_requested = False
        if getattr(self.runtime, "mcp", None) is None:
            return
        try:
            names = default_servers()
        except ValueError as error:
            self.view.error(str(error))
            return
        if not names:
            return
        self.start_mcp_task(
            "defaults",
            self.app.enable_mcp_defaults(names),
            status="Enabling default MCP servers…",
            cancelled="Default MCP enable cancelled; remaining servers stay off.",
        )

    def start_mcp_logout(self, name: str) -> None:
        self.start_mcp_task(
            name,
            self.app.logout_mcp(name),
            status=f"Signing out of MCP '{name}'…",
            cancelled=f"MCP '{name}' sign-out cancelled.",
        )

    def start_compact(self, focus: str) -> None:
        from pcode.ui import SYSTEM_COMMAND_LABELS

        self.start_history_task(
            self.runtime.compact(focus),
            # Label the work instead of echoing "/compact <focus>", which
            # reads like the command was typed as part of a prompt.
            label=SYSTEM_COMMAND_LABELS["/compact"],
            detail=focus,
            status="Compacting context…",
            note="Compacting context with the current model. Ctrl+C cancels.",
            done=lambda result: result.description(),
            cancelled="Compaction cancelled; history unchanged.",
            failed="Compaction failed",
        )

    def start_summary(self, request) -> None:
        # Checked before the task marks the session busy, which would refuse it.
        follows = self.app.check_bridge(request.thread)
        self.start_history_task(
            self.app.summarize_thread(follows, request.instructions),
            label="Summarizing side thread",
            detail=request.instructions,
            status="Summarizing side thread…",
            note="Summarizing the side thread into the conversation. Ctrl+C cancels.",
            done=lambda result: "Side thread summary added to the conversation.",
            cancelled="Summary cancelled; the conversation is unchanged.",
            failed="Side thread summary failed",
        )

    def start_history_task(
        self, work, *, label, detail, status, note, done, cancelled, failed
    ) -> None:
        """Run work that rewrites the conversation's history, holding prompts back.

        Prompts typed meanwhile queue behind it on `compact_idle`, the way a
        turn would, so nothing reads the history while it changes. `done`
        turns the result into the closing note.
        """
        self.compact_idle.clear()
        self.activity.busy = True
        self.activity.status = status
        self.activity.start_prompt(label, kind="system", detail=detail)
        self.view.note(note)

        def finished(task):
            success = False
            try:
                result = task.result()
                self.view.note(done(result))
                self.activity.finish_prompt("done")
                success = True
            except asyncio.CancelledError:
                self.activity.finish_prompt("cancelled")
                self.view.warning(cancelled)
            except Exception as error:
                from pcode.live import error_message

                self.activity.finish_prompt("failed")
                self.view.error(error_message(error), title=failed)
            finally:
                if not success:
                    self.clear_queue()
                self.compact_task = None
                self.refresh_busy()
                self.activity.status = ""
                self.compact_idle.set()
                self.view.redraw()

        self.compact_task = asyncio.create_task(work)
        self.compact_task.add_done_callback(finished)

    # Jobs: rows in the live panel, a watched tail, exits, and wake-ups

    async def watch_jobs(self) -> None:
        """Keep the jobs rows current, and report exits once the turn is over.

        Running rows update busy or idle and disappear on completion.
        Scrollback waits for idle, because a completion written mid-turn
        would land inside the model's streaming text.
        The model is told separately, at its next request, unless nothing
        is going to make one: then the job's notice starts the turn itself.
        Polling here costs one small file read per running job and
        replaces the model doing the same thing with `sleep`.
        """
        await self.ready.wait()
        self.adopt_jobs()
        while True:
            changed = False
            if not self.activity.busy and not self.activity.queued_prompts:
                changed = bool(self.report_finished_jobs())
                prompt = self.wake_prompt() if self.live_task is None else None
                if prompt is not None:
                    self.submit(prompt, "wake")
            # Refresh even while busy so completed jobs leave the live panel.
            if self.refresh_jobs() or changed:
                self.view.redraw()
            await asyncio.sleep(1)

    def refresh_jobs(self) -> bool:
        """Recompute the jobs rows and the watched tail. Returns whether they changed.

        Only running jobs belong here. Completion notices stay pending until
        the turn ends (or the idle watcher reports them) without keeping a row.
        """
        registry = getattr(self.runtime, "jobs", None)
        if registry is None:
            return False
        registry.refresh()
        rows = []
        for job in sorted(registry.jobs.values(), key=lambda job: job.started_at):
            elapsed = format_duration(job.elapsed)
            if job.running and not job.waiting:
                rows.append(
                    ("class:activity.job", f"\u27f3 {job.id} \u00b7 {job.label()} \u00b7 {elapsed}")
                )
        changed = rows != self.activity.jobs
        self.activity.jobs = rows
        return self._refresh_watched(registry) or changed

    def _refresh_watched(self, registry) -> bool:
        from pcode.shell import preview_text

        watched = self.activity.watched_job
        job = registry.get(watched) if watched else None
        if job is None or not job.running:
            self.activity.watched_job = ""
            return self._pin(None)
        tail, _ = registry.read_output(job, max_bytes=OUTPUT_TAIL_BYTES // 2)
        return self._pin(
            CommandOutput(WATCHED_PREFIX + job.id, job.command, preview_text(tail, final=True))
        )

    def _pin(self, event: CommandOutput | None) -> bool:
        """Show `event` as the watched job's preview, replacing the last one."""
        if event == self.pinned:
            return False
        if self.pinned is not None and (event is None or event.call_id != self.pinned.call_id):
            self.view.drop_output(self.pinned.call_id)
        self.pinned = event
        if event is not None:
            self.view.show_output(event)
        return True

    def adopt_jobs(self) -> None:
        """Take over what an earlier pcode left running, and say so."""
        registry = getattr(self.runtime, "jobs", None)
        adopted = registry.adopt_orphans() if registry is not None else []
        if adopted:
            self.view.note(
                f"Adopted {len(adopted)} job{'s' if len(adopted) != 1 else ''} "
                "still running from an earlier pcode; /jobs lists them."
            )
            for job in adopted:
                self.view.note(job.summary())

    def jobs_arguments(self) -> tuple[str, ...]:
        """Complete `stop`/`watch` against jobs still running this session knows about."""
        registry = getattr(self.runtime, "jobs", None)
        if registry is None:
            return ()
        registry.refresh()
        running = sorted(
            (job for job in registry.jobs.values() if job.running), key=lambda job: job.started_at
        )
        return (
            "unwatch",
            "stop all",
            *(f"stop {job.id}" for job in running),
            *(f"watch {job.id}" for job in running),
        )

    async def jobs(self, argument: str) -> None:
        """Browse, watch, or stop the shell jobs this session started.

        Jobs outlive the turn that started them and, deliberately, the session
        itself, so the only way to know what is still running is to ask. Bare
        `/jobs` opens the browser; the subcommands act without it.
        """
        registry = getattr(self.runtime, "jobs", None)
        if registry is None:
            raise ValueError("/jobs requires a live model session.")
        registry.refresh()
        action, _, target = argument.partition(" ")
        # `list` predates the browser; it still opens it rather than failing.
        if not action or action == "list":
            if not registry.jobs:
                self.view.note("No jobs have been started.")
                return
            await self.view.browse_jobs()
            return
        if action == "unwatch":
            self.watch_job(None)
            return
        if action == "watch":
            job = registry.get(target.strip())
            if job is None:
                raise ValueError(f"No job {target.strip()!r}. Run /jobs to browse them.")
            if not job.running:
                raise ValueError(f"[{job.id}] has finished; nothing to watch.")
            self.watch_job(job.id)
            self.view.note(f"Watching [{job.id}] {job.label()}; /jobs unwatch hides it.")
            return
        if action != "stop":
            raise ValueError("/jobs takes stop ID, stop all, watch ID, or unwatch.")
        target = target.strip()
        if target == "all":
            self.stop_jobs(None)
        elif registry.get(target) is not None:
            self.stop_jobs([target])
        else:
            raise ValueError(f"No job {target!r}. Run /jobs to browse them.")

    def watch_job(self, job_id: str | None) -> None:
        """Pin a running job's output tail into the preview, or unpin with None."""
        self.activity.watched_job = job_id or ""
        self.refresh_jobs()

    def stop_jobs(self, job_ids: list[str] | None) -> None:
        """Stop these jobs, or every running one for None, and say so in scrollback."""
        registry = self.runtime.jobs
        jobs = None if job_ids is None else [registry.get(job_id) for job_id in job_ids]
        stopped = registry.stop_all([job for job in jobs if job] if jobs is not None else None)
        for job in stopped:
            # Printed here, so the idle watcher does not repeat it.
            job.announced.add("ui")
            self.view.note(f"Stopped [{job.id}] {job.label()}")
        if not stopped:
            self.view.note("Nothing was running.")
        # Now, not at the watcher's next tick: the rows answer this command.
        self.refresh_jobs()
