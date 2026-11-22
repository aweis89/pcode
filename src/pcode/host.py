"""A headless process that owns one conversation, for terminals to attach to.

The host runs a `SessionController` exactly as a terminal would in-process,
but renders nothing: its view, `HostView`, sends every call to the attached
terminals over `pcode.rpc`, and keeps the calls since the running turn began
so a terminal attaching mid-turn can catch up. Terminals come and go; the host
stops when told to, when the session ends itself, or after an idle timeout.

Started by `pcode.remote.spawn_host`, which runs `python -m pcode.host` from the
terminal that asked for it, so the host inherits that terminal's environment
(direnv credentials, `PATH`) the way a local session would.

The connection starts with one plain line each way (`hello`, then `ready` or
`error`), so a host and terminal on different protocol versions refuse each
other with a message instead of hanging. Then both ends are `rpc.Peer`s: the
terminal calls `attach` for its `welcome`, sends intents (`INTENTS`) and
queries, and receives view calls.
"""

import argparse
import asyncio
import itertools
import os
import signal
import sys
import time
from pathlib import Path

from pcode import remote_profile
from pcode.controller import INTENTS, SESSION_FIELDS, SessionController
from pcode.error_report import error_message
from pcode.host_protocol import (
    LINE_LIMIT,
    NOTHING_TO_RESUME,
    PROTOCOL,
    HostEntry,
    code_fingerprint,
    dumps,
    forget_stopped,
    host_dir,
    new_host_id,
    read_message,
    remember_stopped,
    remove_entry,
    socket_path,
    write_entry,
)
from pcode.jobs import registry as job_registry
from pcode.rpc import Peer
from pcode.runtime import CommandOutput, EditPreview, TextDelta, ThinkingDelta
from pcode.ui import Activity

# Consecutive events of these kinds merge in the catch-up buffer: deltas join,
# and a newer snapshot of the same call replaces the older one.
DELTA_EVENTS = (TextDelta, ThinkingDelta)

# How long a host with nothing to do waits for a terminal before stopping, when
# `session_host_idle_minutes` is 0: long enough to /attach away and back.
IDLE_GRACE_SECONDS = 15.0
# How often drawers watching the side questions hear of changes (`pcode.drawer`).
OBSERVER_INTERVAL = 0.08
SNAPSHOT_EVENTS = (CommandOutput, EditPreview)

# View calls that change what the conversation is, so a terminal attaching
# later reads it from the journal instead of from the buffer.
RESETS = {"conversation_reset", "replay_conversation", "show_branch"}

# What a terminal may call besides the controller's intents.
HOST_CALLS = frozenset(
    {
        "attach",
        "peek",
        "ask_aside",
        "query",
        "send",
        "run",
        "stop",
        "stop_if_idle",
        "restart",
        "asides_read",
    }
)

_MISSING = object()


class MirroredActivity(Activity):
    """The host's live-panel state; each change to a session field goes to the terminals."""

    def __init__(self, send) -> None:
        object.__setattr__(self, "_send", None)
        object.__setattr__(self, "_sent", {})
        super().__init__()
        object.__setattr__(self, "_send", send)

    def __setattr__(self, name: str, value) -> None:
        object.__setattr__(self, name, value)
        if name in SESSION_FIELDS and self._send is not None:
            self.push()

    def push(self) -> None:
        """Send the session fields that changed since the last push; lists compare by value."""
        changes = {}
        for name in SESSION_FIELDS:
            value = getattr(self, name)
            if isinstance(value, list):
                value = list(value)
            if self._sent.get(name, _MISSING) != value:
                self._sent[name] = value
                changes[name] = value
        if changes:
            self._send(changes)

    def session_fields(self) -> dict:
        return {name: getattr(self, name) for name in SESSION_FIELDS}


class _Run:
    """A command sent with `run`, until it has run."""

    def __init__(self) -> None:
        self.done = asyncio.get_running_loop().create_future()
        # The background tasks (compaction, MCP) as it began; None until it does.
        self.work: tuple | None = None


class _Owner:
    """A headless caller's queued message: when it starts, or that it never will.

    Told to that caller alone, in order with the view calls, so everything
    between `message_started` and `after_turn` is its turn.
    """

    def __init__(self, client: "_Client") -> None:
        self.client = client

    def started(self) -> None:
        self.client.peer.notify("message_started")

    def dropped(self) -> None:
        self.client.peer.notify("message_dropped")


class _Client:
    """One attached terminal, and the calls it may make."""

    def __init__(self, host: "SessionHost", number: int) -> None:
        self.host = host
        self.number = number
        self.peer: Peer | None = None
        # A caller with no editor (`--attach --print`): popups and side-answer
        # notices meant for "the terminal last used" skip it, and it does not
        # count as a terminal watching the session.
        self.headless = False
        # What attaching took from the terminals, given back if it turns out headless.
        self.displaced: _Client | None = None
        self.unseen_before = False

    # Intents: the controller's, with commands tagged by who sent them.

    def submit(self, text: str, mode: str) -> None:
        if mode == "shell" and remote_profile.active() is not None:
            # A `!command` runs outside the sandbox; an unattended host takes none,
            # whoever connects (its own model's commands cannot connect at all).
            raise PermissionError("Shell mode is off in a remote session.")
        self.host.touch(self)
        self.host.controller.submit(text, mode)

    def command(self, text: str, tag=None) -> None:
        self.host.touch(self)
        self.host.controller.command(text, (self.number, tag))

    # Host calls for a caller with no editor to watch

    def send(self, text: str) -> None:
        """Queue `text` as a turn of its own; the caller hears when it starts (see `_Owner`)."""
        self.become_headless()
        self.host.active_at = time.monotonic()
        self.host.controller.submit(text, "queue", owner=_Owner(self))

    async def run(self, text: str) -> bool:
        """`command`, returning once it has run, and whether it did.

        A command dropped from the queue (a Ctrl+C elsewhere cleared it) or
        refused (one that waits for an idle session) did not. Background work
        the command started (compaction, MCP sign-in) is waited for, as its
        outcome is the point of sending, say, `/compact`.
        """
        self.become_headless()
        self.host.active_at = time.monotonic()
        tag = (self.number, f"run-{next(self.host.run_ids)}")
        run = self.host.runs[tag] = _Run()
        try:
            self.host.controller.command(text, tag)
            await run.done
        finally:
            self.host.runs.pop(tag, None)
        if run.work is None:
            return False
        controller = self.host.controller
        compaction, mcp = run.work
        if controller.compact_task not in (None, compaction):
            await controller.compact_idle.wait()
        if controller.mcp_task not in (None, mcp):
            await controller.mcp_idle.wait()
        return True

    def become_headless(self) -> None:
        """This caller has no editor: give the terminals back what attaching took."""
        if self.headless:
            return
        self.headless = True
        host = self.host
        if host._latest is self:
            host._latest = self.displaced
        host.update(attached=len(host.terminals()), unseen=host.entry.unseen or self.unseen_before)

    def cancel(self) -> None:
        self.host.controller.cancel()

    def set_thinking(self, mode: str) -> None:
        self.host.controller.set_thinking(mode)

    def adjust_effort(self, direction: int) -> None:
        self.host.controller.adjust_effort(direction)
        self.host.controller.view.session_changed()

    def stop_jobs(self, job_ids) -> None:
        self.host.controller.stop_jobs(job_ids)

    def cancel_asides(self) -> None:
        self.host.controller.cancel_asides()

    def watch_job(self, job_id) -> None:
        self.host.controller.watch_job(job_id)

    # Host calls

    def attach(self) -> dict:
        """Everything needed to show the session now; later changes follow as view calls.

        Built and registered with no await in between, so every change arrives
        exactly once: in this welcome or after it.
        """
        return self.host.attach(self)

    async def peek(self) -> dict:
        """Read-only live snapshot without attaching, clearing `unseen`, or reading scrollback."""
        return await self.host.peek()

    async def ask_aside(self, question: str, model: str = "") -> dict:
        """One-shot headless `/btw` against this host's context; records nothing."""
        return await self.host.ask_aside(question, model)

    async def query(self, name: str, *args):
        return await self.host.controller.query(name, *args)

    def asides_read(self, identities) -> None:
        """The terminal showed these answers; no terminal needs telling they are ready."""
        for aside in self.host.controller.asides.items:
            if aside.id in identities and not aside.read:
                aside.read = True
                self.host.emit_aside(aside)

    def stop(self, keep_worktree: bool = False) -> None:
        # A remote profile keeps its worktree however the host is stopped.
        self.host.keep_worktree = bool(keep_worktree) or remote_profile.active() is not None
        self.host.stop()

    def stop_if_idle(self, require_detached: bool = False, keep_worktree: bool = False) -> str:
        """`pcode --kill-hosts idle`: "" once stopping, else what it is busy with."""
        return self.host.stop_if_idle(
            require_detached=require_detached, keep_worktree=keep_worktree
        )

    def restart(self) -> dict:
        """`/restart all`: `{"id": successor}` once stopping, else `{"skipped": why not}`."""
        return self.host.restart()


class HostView:
    """The controller's view in a host: calls go to the attached terminals.

    Plain calls go to every terminal and into the catch-up buffer. Popups and
    commands handed back go to the terminal whose command is running (a
    command's tag carries its sender), or else the one that sent the last
    thing; with no terminal attached they are answered with nothing.
    """

    def __init__(self, host: "SessionHost") -> None:
        self._host = host
        # The running command's tag as its terminal gave it (see `read_asides`).
        self._command_tag = None
        # Side-answer viewers open in a terminal (see `read_asides`), and
        # what they asked for while it is kept.
        self.viewers: set[asyncio.Task] = set()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self._host.emit(name, args, kwargs)

        return call

    # Turns: the buffer restarts where a turn begins.

    def turn_started(self, text: str, *, echo: bool) -> None:
        self._host.turn_began(text)
        self._host.emit("turn_started", (text,), {"echo": echo})

    async def after_turn(self) -> None:
        self._host.turn_finished()
        self._host.emit("after_turn", (), {})

    async def after_command(self, tag=None) -> None:
        # Terminals get it untagged, as ever; a `run` caller is waiting for its own.
        self._host.emit("after_command", (), {})
        run = self._host.run_for(tag)
        if run is not None and not run.done.done():
            run.done.set_result(None)
        if not self._host.controller.running:
            # Only the terminal that sent it: `sender` would guess for an untagged one.
            client = self._host.sender(tag)[0] if isinstance(tag, (list, tuple)) else None
            if client is not None:
                # Ahead of `host_closed`, which can overtake the reply to `run`:
                # ending the session was what this caller asked for, so its
                # terminal exits as an in-process one would, not as after a crash.
                client.peer.notify("session_ended")
            self._host.stop()

    def session_changed(self) -> None:
        self._host.push_state()

    def commands_changed(self) -> None:
        self._host.push_state()

    def redraw(self) -> None:
        pass  # Terminals repaint on what they receive.

    def aside_changed(self, aside) -> None:
        self._host.emit_aside(aside)

    def aside_answered(self, aside) -> None:
        self._host.emit_aside(aside)
        client = self._host.latest_client()
        if client is not None:
            client.peer.notify("aside_answered", self._host.aside_state(aside))

    # Requests to one terminal

    async def run_command(self, text: str, *, idle: bool, tag) -> None:
        self._host.run_began(tag)
        client, own_tag = self._host.sender(tag)
        if client is None:
            return
        try:
            await client.peer.request("run_command", text, idle=idle, tag=own_tag)
        except ConnectionError:
            pass

    def command_started(self, tag) -> None:
        self._host.run_began(tag)
        client, own_tag = self._host.sender(tag)
        self._host.running_command = client
        self._command_tag = own_tag
        if client is not None:
            client.peer.notify("command_started", own_tag)

    def command_finished(self) -> None:
        client, self._host.running_command = self._host.running_command, None
        self._command_tag = None
        if client is not None and client.peer is not None:
            client.peer.notify("command_finished")

    async def _ask(self, method: str, *args):
        client = self._host.running_command or self._host.latest_client()
        if client is None:
            self._host.emit("note", (f"{method} needs an attached terminal.",), {})
            return None
        try:
            return await client.peer.request(method, *args)
        except ConnectionError:
            return None

    async def choose_model(self, values, providers, current):
        return await self._ask("choose_model", list(values), sorted(providers), current)

    async def read_asides(self):
        """Open the side-answer viewer in the asking terminal, without waiting for it.

        The viewer stays open as long as its reader likes, and the host runs
        one command at a time: awaited here, one terminal's open viewer would
        stall every other terminal's commands behind it. So the command ends
        at once, and a thread the viewer asks to bring into the conversation
        is kept from outside the queue, as the swap view keeps one (`keep_thread`).
        """
        client = self._host.running_command
        if client is None or client.headless:
            # A headless caller only says it cannot show the viewer, at once
            # and while its command still runs, so `run` reports it failed.
            return await self._ask("read_asides")
        task = asyncio.create_task(self._read_asides(client, self._command_tag))
        self.viewers.add(task)
        task.add_done_callback(self.viewers.discard)
        return None

    async def _read_asides(self, client: "_Client", tag) -> None:
        # The tag goes along: the command has finished by the time the viewer
        # opens, and the terminal needs it to tell an auto-open (see `PreviewApp.read_asides`).
        try:
            request = await client.peer.request("read_asides", tag)
        except ConnectionError:
            return
        if request is None:
            return
        controller = self._host.controller
        try:
            await controller.keep_thread(request.thread, request.action, request.instructions)
        except Exception as error:
            controller.command_failed("/btw", error)

    async def browse_jobs(self) -> None:
        await self._ask("browse_jobs")

    async def read_secret(self, title: str, prompt: str, detail: str = "") -> str | None:
        return await self._ask("read_secret", title, prompt, detail)


class SessionHost:
    """Serves one controller to the terminals attached to it."""

    def __init__(self, entry: HostEntry, directory: Path | None = None) -> None:
        self.entry = entry
        self.directory = directory or host_dir()
        self.clients: dict[int, _Client] = {}
        self._numbers = itertools.count(1)
        self._latest: _Client | None = None
        self.running_command: _Client | None = None
        # Commands sent with `run`, by tag, until they have run.
        self.runs: dict[tuple, _Run] = {}
        self.run_ids = itertools.count(1)
        self.view = HostView(self)
        self.activity = MirroredActivity(lambda changes: self.emit("state", (changes,), {}))
        self.controller = SessionController(self.view, self.activity)
        self.controller.interactive = True
        # A loop task cancelled while the host shuts down is not a Ctrl+C.
        self.controller.closing = lambda: not self.controller.running
        # Everything shown since the journal offset `settled_end`: a terminal
        # attaching reads the journal up to there and replays these after it.
        self.buffer: list[tuple[str, tuple, dict]] = []
        self.settled_end: int | None = None
        self._state: dict = {}
        self.stopped = asyncio.Event()
        # A restart resumes this session in its worktree, so stopping must not tidy it away.
        self.keep_worktree = False
        self.server: asyncio.AbstractServer | None = None
        self.tasks: list[asyncio.Task] = []
        # When the host last had a terminal or work; the idle clock starts here.
        self.active_at = time.monotonic()
        # Stopped for being idle, not asked to: an unseen result is remembered.
        self.idle_stopped = False
        # The host `restart` asked to carry the session on once this one has gone.
        self.successor: str | None = None
        # Open connections, counted from hello on: a terminal mid-handshake is not
        # a client yet, and must not find the host gone.
        self.connections = 0
        # In-flight headless `ask_aside` RPCs, counted in `busy_with()`.
        self._host_asides = 0
        # A remote profile's per-turn wall clock, while a turn runs.
        self._turn_clock: asyncio.TimerHandle | None = None
        # Drawers watching the side questions (`pcode.drawer`): not terminals,
        # not connections, and allowed to call nothing.
        self.observers: set[Peer] = set()
        # Side questions changed since observers last heard, sent together
        # shortly after, so a streaming answer is not rendered per token.
        self._observed: dict[str, object] = {}
        self._observe_flush: asyncio.TimerHandle | None = None

    @property
    def socket(self) -> Path:
        return socket_path(self.entry.id, self.directory)

    @property
    def busy(self) -> bool:
        return bool(self.activity.busy or self.controller.working())

    # Serving

    async def serve(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.socket
        path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._accept, path=path, limit=LINE_LIMIT)
        os.chmod(path, 0o600)
        self.update(state="idle")
        if self.entry.session_id:
            # Running again, so /attach lists the host instead of the stopped one.
            forget_stopped(self.entry.session_id, self.directory)

    async def boot(self) -> None:
        """Build the runtime, say what a new session says, then start the loops.

        Terminals may attach while this runs: they see the startup notes as
        they come, and anything they send waits for the loops.
        """
        controller = self.controller
        try:
            await controller.initialize_runtime()
        except Exception as error:  # noqa: BLE001 - reported to the terminals.
            controller.startup_error = error
            self.view.error(error_message(error), title="Agent startup failed")
            print(f"startup failed: {error_message(error)}", file=sys.stderr, flush=True)
        finally:
            controller.startup_pending = False
        # A resumed conversation is in the journal already; what follows is not.
        self.reset_buffer()
        if controller.startup_error is None:
            if controller.resuming:
                # Terminals attached while it loaded draw it now (the state
                # first, so they know which journal to read).
                self.push_state()
                self.view.replay_conversation()
            controller.show_startup_context()
            controller.warn_without_credentials()
            # Optional, and it can hang; the session must not wait for it.
            self.tasks.append(asyncio.create_task(self._refresh_context()))
            self.tasks.append(asyncio.create_task(controller.warn_outdated()))
            profile = remote_profile.active()
            if profile is None or profile.mcp:
                controller.start_mcp_defaults()
        self.push_state()
        self.start()

    async def _refresh_context(self) -> None:
        refresh = getattr(self.controller.runtime, "refresh_context", None)
        if refresh is None:
            return
        try:
            await refresh()
        except Exception as error:  # noqa: BLE001 - metadata only.
            print(f"context metadata unavailable: {error}", file=sys.stderr, flush=True)
        self.push_state()

    def start(self) -> None:
        """Run the controller's loops: turns, commands, and background jobs."""
        controller = self.controller
        for work in (controller.consume(), controller.consume_commands(), controller.watch_jobs()):
            self.tasks.append(asyncio.create_task(work))
        controller.ready.set()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            await self._handshake(reader, writer)
        finally:
            self.connections -= 1

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            hello = await read_message(reader)
        except ValueError:
            hello = None
        if hello is not None and hello.get("type") == "observe":
            await self._observe(hello, reader, writer)
            return
        if hello is None or hello.get("type") != "hello":
            writer.close()
            return
        if hello.get("protocol") != PROTOCOL:
            writer.write(
                dumps(
                    {
                        "type": "error",
                        "message": f"This session host speaks protocol {PROTOCOL}, the terminal "
                        f"{hello.get('protocol')}. Restart one of them on the same pcode.",
                    }
                )
            )
            await writer.drain()
            writer.close()
            return
        writer.write(dumps({"type": "ready", "protocol": PROTOCOL}))
        client = _Client(self, next(self._numbers))
        client.peer = Peer(reader, writer, client, allowed=INTENTS | HOST_CALLS)
        try:
            await client.peer.serve()
        except ValueError, ConnectionError:
            pass
        finally:
            client.peer.close()
            if self.clients.pop(client.number, None) is not None:
                self.active_at = time.monotonic()
                if self._latest is client:
                    self._latest = next(reversed(self.clients.values()), None)
                if self.running_command is client:
                    self.running_command = None
                if not self.stopped.is_set():
                    self.update(attached=len(self.terminals()))

    async def _observe(
        self, hello: dict, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """A drawer showing the side questions (`pcode.drawer`), until it goes.

        It gets every side question, then each change. It may call nothing,
        and it is not a terminal: it neither keeps an idle host running nor
        marks a finished turn seen.
        """
        from pcode import drawer

        if hello.get("version") != drawer.OBSERVER_VERSION:
            message = "This session host's pcode does not speak this drawer's version."
            writer.write(dumps({"type": "error", "message": message}))
            await writer.drain()
            writer.close()
            return
        writer.write(dumps({"type": "ready", "version": drawer.OBSERVER_VERSION}))
        peer = Peer(reader, writer, object(), allowed=frozenset())
        counted = False
        try:
            self.observers.add(peer)
            asides = self.controller.asides.items
            peer.notify("asides", [self.aside_state(aside) for aside in asides])
            # Counted from hello on, as every connection; no longer once it is a drawer.
            self.connections -= 1
            counted = True
            await peer.serve()
        except ValueError, ConnectionError:
            pass
        finally:
            if counted:
                self.connections += 1
            self.observers.discard(peer)
            peer.close()

    def _notify_observers(self) -> None:
        self._observe_flush = None
        changed, self._observed = list(self._observed.values()), {}
        for aside in changed:
            state = self.aside_state(aside)
            for peer in list(self.observers):
                peer.notify("aside_changed", state)

    def attach(self, client: _Client) -> dict:
        from pcode.rpc import encode

        self.active_at = time.monotonic()
        self.clients[client.number] = client
        client.displaced, self._latest = self._latest, client
        client.unseen_before = self.entry.unseen
        # Shown now, so whatever finished while nobody watched has been seen.
        self.update(attached=len(self.terminals()), unseen=False)
        self._state = self.controller.session_state()
        return {
            "id": self.entry.id,
            "pid": os.getpid(),
            "session": self._state,
            "activity": self.activity.session_fields(),
            "asides": [self.aside_state(aside) for aside in self.controller.asides.items],
            "settled_end": self.settled_end,
            "forked_from": getattr(
                getattr(self.controller.runtime, "session", None), "forked_from", None
            )
            or "",
            "fork_requested": getattr(
                getattr(self.controller.runtime, "session", None), "fork_requested", False
            ),
            # Already encoded: a buffered call's arguments are whatever the view got.
            "calls": [
                [name, encode(list(args)), encode(kwargs)] for name, args, kwargs in self.buffer
            ],
        }

    async def peek(self) -> dict:
        """Read-only live snapshot: state, plan, running jobs, asides, and activity.

        Does not register as a terminal, change `attached`, clear `unseen`, or
        serialize the scrollback catch-up buffer.
        """
        controller = self.controller
        runtime = controller.runtime
        plan_store = getattr(runtime, "plan_store", None)
        plan = (
            [item.model_dump(mode="json") for item in await plan_store.get_items()]
            if plan_store is not None
            else []
        )
        jobs_reg = getattr(runtime, "jobs", None)
        jobs = (
            [
                {
                    "id": job.id,
                    "command": job.command,
                    "purpose": job.purpose,
                    "adopted": job.adopted,
                }
                for job in jobs_reg.running()
            ]
            if jobs_reg is not None
            else []
        )
        asides = controller.asides
        return {
            "id": self.entry.id,
            "session_id": self.entry.session_id,
            "workspace": self.entry.workspace,
            "model": controller.model or self.entry.model,
            "state": self.entry.state,
            "title": self.entry.title,
            "last_prompt": self.entry.last_prompt,
            "turns": self.entry.turns,
            "outcome": self.entry.outcome,
            "attached": len(self.terminals()),
            "unseen": self.entry.unseen,
            "held": self.entry.held,
            "busy_with": self.busy_with(),
            "startup_pending": controller.startup_pending,
            "startup_error": (
                error_message(controller.startup_error) if controller.startup_error else ""
            ),
            "activity": {
                "busy": self.activity.busy,
                "status": self.activity.status,
                "queued": self.activity.queued,
                "queued_prompts": list(self.activity.queued_prompts),
                "prompt": self.activity.prompt,
                "prompt_state": self.activity.prompt_state,
                "turn_outcome": self.activity.turn_outcome,
                "job_count": self.activity.job_count,
            },
            "plan": plan,
            "jobs": jobs,
            "asides": {
                "total": len(asides.items),
                "running": asides.running + self._host_asides,
                "unread": asides.unread,
                "recent": [
                    {
                        "id": aside.id,
                        "question": aside.question,
                        "status": aside.status,
                        "read": aside.read,
                    }
                    for aside in asides.items[-5:]
                ],
            },
        }

    async def ask_aside(self, question: str, model: str = "") -> dict:
        """Answer `question` beside this conversation without recording an aside."""
        from pcode.aside import ASIDE_TIMEOUT_SECONDS, SideTarget, split_effort

        cleaned = question.strip()
        if not cleaned:
            raise ValueError("Provide a non-empty question.")
        controller = self.controller
        self._host_asides += 1
        try:
            async with asyncio.timeout(ASIDE_TIMEOUT_SECONDS):
                if controller.startup_pending:
                    await controller.ready.wait()
                if controller.startup_error is not None:
                    raise RuntimeError(
                        f"Agent startup failed: {error_message(controller.startup_error)}"
                    )
                runtime = controller.runtime
                if getattr(runtime, "aside", None) is None:
                    raise RuntimeError("This session host cannot answer side questions.")
                self.active_at = time.monotonic()
                chosen_model = model.strip()
                options = {}
                if chosen_model:
                    name, effort = split_effort(chosen_model)
                    target = SideTarget(name, effort)
                    controller.check_effort(target)
                    options = await controller.target_options(target)
                reply = await runtime.aside(cleaned, **options)
        finally:
            self._host_asides -= 1
        self.active_at = time.monotonic()
        return {
            "host_id": self.entry.id,
            "session_id": self.entry.session_id,
            "state": self.entry.state,
            "model": chosen_model or controller.model or self.entry.model,
            "answer": reply.answer,
        }

    def touch(self, client: _Client) -> None:
        self._latest = client
        self.active_at = time.monotonic()

    def terminals(self) -> list[_Client]:
        """The attached clients with an editor, in attach order: not headless callers."""
        return [client for client in self.clients.values() if not client.headless]

    def latest_client(self) -> _Client | None:
        """The terminal last used, for what goes to one terminal; never a headless caller."""
        latest = self._latest
        if latest is not None and self.clients.get(latest.number) is latest and not latest.headless:
            return latest
        # Attach order: the newest terminal that has an editor.
        return next(reversed(self.terminals()), None)

    def sender(self, tag) -> tuple[_Client | None, object]:
        """The terminal a command came from, and the tag it gave it."""
        if isinstance(tag, (list, tuple)) and len(tag) == 2:
            return self.clients.get(tag[0]), tag[1]
        return self.latest_client(), None

    def run_for(self, tag) -> _Run | None:
        # Only `run` tags are strings: a terminal's own tag may not even be hashable.
        if isinstance(tag, tuple) and len(tag) == 2 and isinstance(tag[1], str):
            return self.runs.get(tag)
        return None

    def run_began(self, tag) -> None:
        """A `run` command is being handled: note the background work already going."""
        if (run := self.run_for(tag)) is not None:
            run.work = (self.controller.compact_task, self.controller.mcp_task)

    # Showing

    def emit(self, method: str, args: tuple, kwargs: dict) -> None:
        """Send a view call to every terminal and keep it for the ones that attach later."""
        if method in RESETS:
            self.reset_buffer()
        if method == "conversation_reset":
            self.update(session_id="", forked_from="", title="", last_prompt="", outcome="")
            self.own_jobs()
        for client in list(self.clients.values()):
            client.peer.notify(method, *args, **kwargs)
        if method in RESETS:
            return
        self._buffer(method, args, kwargs)

    def _buffer(self, method: str, args: tuple, kwargs: dict) -> None:
        previous = self.buffer[-1] if self.buffer else None
        if method == "turn_event" and previous is not None and previous[0] == "turn_event":
            event, before = args[0], previous[1][0]
            if type(event) is type(before) and isinstance(event, DELTA_EVENTS):
                merged = type(event)(before.text + event.text)
                self.buffer[-1] = ("turn_event", (merged,), previous[2])
                return
            if (
                type(event) is type(before)
                and isinstance(event, SNAPSHOT_EVENTS)
                and event.call_id == before.call_id
            ):
                self.buffer[-1] = (method, args, kwargs)
                return
        self.buffer.append((method, args, kwargs))

    def reset_buffer(self) -> None:
        """The journal now holds everything shown so far; start the buffer again."""
        session = getattr(self.controller.runtime, "session", None)
        self.settled_end = session.journal_size() if session is not None else None
        self.buffer = []

    def aside_state(self, aside) -> dict:
        """A side question as a terminal needs it: everything but the reply it continues from."""
        return {
            "id": aside.id,
            "question": aside.question,
            "model": aside.model,
            "label": aside.label,
            "effort": aside.effort,
            "status": aside.status,
            "answer": aside.answer,
            "activity": aside.activity,
            "error": aside.error,
            # Monotonic clock readings, which are system-wide: comparable in the terminal.
            "started": aside.started,
            "finished": aside.finished,
            "thread": aside.thread,
            "conversation": aside.conversation,
            "base": aside.base,
            "bridged": aside.bridged,
            # Whether it can be followed up: a reply held here, or a saved node.
            "replied": aside.continuable,
            "read": aside.read,
        }

    def emit_aside(self, aside) -> None:
        for client in list(self.clients.values()):
            client.peer.notify("aside_changed", self.aside_state(aside))
        if self.observers:
            self._observed[aside.id] = aside
            if self._observe_flush is None:
                self._observe_flush = asyncio.get_running_loop().call_later(
                    OBSERVER_INTERVAL, self._notify_observers
                )

    def push_state(self) -> None:
        """Send the session state if it changed since the last one sent."""
        state = self.controller.session_state()
        if state != self._state:
            self._state = state
            for client in list(self.clients.values()):
                client.peer.notify("session_state", state)
        session = getattr(self.controller.runtime, "session", None)
        if session is not None and session.info.id != self.entry.session_id:
            self.update(session_id=session.info.id, forked_from=session.info.forked_from or "")
            forget_stopped(session.info.id, self.directory)
            self.own_jobs()
        # /attach lists the session by its name once it has one, not its first prompt.
        if (title := state.get("session_title")) and title != self.entry.title:
            self.update(title=title)

    def own_jobs(self) -> None:
        """Tag the jobs this host launches with its session, and take back that session's.

        Jobs a /detach or an earlier host of this session left running are this
        conversation's, not adopted strangers: they keep the host up and wake it.
        """
        jobs = getattr(self.controller.runtime, "jobs", None)
        (job_registry() if jobs is None else jobs).session = self.entry.session_id

    # The entry other terminals read

    def update(self, **changes) -> None:
        for key, value in changes.items():
            setattr(self.entry, key, value)
        write_entry(self.entry, self.directory)

    def turn_began(self, prompt: str) -> None:
        self.reset_buffer()
        changes: dict[str, str] = {"state": "working"}
        if prompt:
            changes["last_prompt"] = prompt
            changes["title"] = self.entry.title or prompt
        self.update(**changes)
        profile = remote_profile.active()
        if profile is not None:
            remote_profile.BUDGET.start()
            if profile.turn_minutes:
                self._turn_clock = asyncio.get_running_loop().call_later(
                    profile.turn_minutes * 60, self.turn_expired
                )

    def turn_expired(self) -> None:
        """The remote profile's wall clock ran out: stop the turn and say why."""
        self._turn_clock = None
        controller = self.controller
        task = controller.live_task
        if task is None or task.done():
            return
        self.view.warning(remote_profile.BUDGET.expire())
        # As Ctrl+C does: commands the turn is waiting on stop with it.
        controller.set_cancel_policy("stop")
        task.cancel()

    def turn_finished(self) -> None:
        if self._turn_clock is not None:
            self._turn_clock.cancel()
            self._turn_clock = None
        outcome = {"done": "done", "failed": "failed", "cancelled": "cancelled"}.get(
            self.activity.prompt_state, "done"
        )
        self.active_at = time.monotonic()
        self.push_state()
        self.update(
            state="idle",
            outcome=outcome,
            turns=self.entry.turns + 1,
            # A headless `--attach -p` caller printed the reply, so it was seen.
            unseen=not self.clients,
        )

    # Stopping

    def idle(self) -> bool:
        """Nothing would be lost by stopping: no terminal, no work, no running job."""
        return not self.clients and not self.connections and not self.busy_with()

    def busy_with(self) -> str:
        """What stopping now would interrupt, terminals aside; "" for nothing."""
        controller = self.controller
        jobs = getattr(controller.runtime, "jobs", None)
        work = (
            (self.busy, "a turn or command is running"),
            (not controller.command_idle.is_set(), "a command is running"),
            # A job outlives its host, but its finish would wake nobody. Not one
            # adopted from an exited pcode: it never wakes this session, and
            # stopping leaves it for the next pcode to adopt. Counting it let a
            # daemon some other session started keep a host up for days.
            (
                jobs is not None and any(not job.adopted for job in jobs.running()),
                "a background command is running",
            ),
            (controller.startup_pending, "it is starting up"),
            (
                getattr(controller.runtime, "has_provider_wake", lambda: False)(),
                "Antigravity is continuing background work",
            ),
            (controller.asides.running or self._host_asides, "a side question is running"),
            # A thread brought into the conversation from it would be lost.
            (self.view.viewers, "a side-answer viewer is open"),
            # A title asked for beside the first turn, or a name /rename asked for, would be lost.
            (controller.naming(), "it is naming the session"),
        )
        return next((reason for busy, reason in work if busy), "")

    def stop_if_idle(self, require_detached: bool = False, keep_worktree: bool = False) -> str:
        """Stop as an idle host stops itself; else say what it is busy with.

        When `require_detached` is True, attached terminals also refuse the stop.
        When `keep_worktree` is True, the session's worktree is left untouched.
        Checked and stopped with no await in between, so nothing starts in the gap.
        """
        if require_detached and (attached := len(self.terminals())):
            return f"{attached} terminal(s) attached"
        if reason := self.busy_with():
            return reason
        if keep_worktree:
            self.keep_worktree = True
        self.idle_stopped = True
        self.stop()
        return ""

    def restart(self) -> dict:
        """Stop, leaving a successor to resume this session on the pcode installed now.

        `main` starts the successor once this host has let go of the session,
        from this process, so it keeps this host's environment and remote
        profile rather than those of whoever asked. Attached terminals do not
        stop it; work in progress does. Checked and stopped with no await in
        between, so nothing starts in the gap.
        """
        if reason := self.busy_with():
            return {"skipped": reason}
        if not self.entry.session_id:
            return {"skipped": NOTHING_TO_RESUME}
        self.successor = new_host_id()
        # The successor resumes in this worktree.
        self.keep_worktree = True
        self.stop()
        return {"id": self.successor}

    async def stop_when_idle(
        self, minutes: float, *, every: float = 2.0, grace: float = IDLE_GRACE_SECONDS
    ) -> None:
        """Stop once idle for `minutes`, or for `grace` seconds when `minutes` is 0.

        Polled rather than told: jobs and side questions end without a hook,
        and reading this state every couple of seconds costs nothing. The
        journal keeps the conversation, and a result nobody saw stays listed
        in /attach (`remember_stopped`).
        """
        limit = minutes * 60 if minutes else grace
        while not self.stopped.is_set():
            await asyncio.sleep(every)
            self.held_by("" if self.clients or self.connections else self.busy_with())
            if not self.idle():
                self.active_at = time.monotonic()
            elif time.monotonic() - self.active_at >= limit:
                print(f"idle for {limit:g} s; stopping", file=sys.stderr, flush=True)
                self.idle_stopped = True
                self.stop()
                return

    def held_by(self, reason: str) -> None:
        """Publish what keeps a host with no terminal from stopping, when it changes.

        A host nobody is attached to is invisible otherwise: /attach shows the
        reason on its row, and the log keeps when it started and ended.
        """
        if reason == self.entry.held:
            return
        note = f"not stopping: {reason}" if reason else "nothing holds it now"
        print(note, file=sys.stderr, flush=True)
        self.entry.held = reason
        # Not activity: /attach ages and orders hosts by `updated`.
        write_entry(self.entry, self.directory, touch=False)

    def stop(self) -> None:
        # At once, not in close(): a prompt an attached terminal sends before
        # then would otherwise start a turn only for close() to cancel it.
        self.controller.running = False
        self.stopped.set()

    async def close(self) -> None:
        controller = self.controller
        controller.running = False
        # Terminals hear that the host is going, not the teardown after it
        # (a "Run cancelled" for a host stopped while idle).
        clients, self.clients = list(self.clients.values()), {}
        for client in clients:
            # Older terminals ignore the argument; newer ones point at the successor.
            client.peer.notify("host_closed", self.successor)
            client.peer.close()
        if self._observe_flush is not None:
            self._observe_flush.cancel()
            self._notify_observers()
        for peer in list(self.observers):
            peer.notify("host_closed")
            peer.close()
        controller.cancel_all()
        viewers = list(self.view.viewers)
        for task in (*self.tasks, *viewers):
            task.cancel()
        await asyncio.gather(*self.tasks, *viewers, *controller.tasks(), return_exceptions=True)
        await controller.asides.close()
        # Stopping them settled them: their journal entries go in before the session closes.
        await controller.side_records_settled()
        await controller.stop_naming()
        if self.server is not None:
            self.server.close()
        if controller.extensions is not None:
            await controller.extensions.close()
        remove_entry(self.entry.id, self.directory)
        if self.idle_stopped and self.entry.unseen and self.successor is None:
            # After the entry goes, so /attach never sees both. Only a saved
            # session has an ID, so only one /resume could reopen.
            remember_stopped(self.entry, self.directory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one pcode conversation headless.")
    parser.add_argument("--id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--resume")
    parser.add_argument("--fork", action="store_true")
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--worktree", nargs="?", const=True)
    parser.add_argument("--no-worktree", action="store_true")
    # JSON from `RemoteProfile.to_json`: an unattended host's fixed limits.
    parser.add_argument("--remote-profile")
    return parser


def remote_resume_workspace(info, workspace: Path, profile) -> Path:
    """Where a remote session resumes: its own worktree, else a fresh one.

    A local resume whose worktree was merged or cleaned away falls back to the
    project checkout. An unattended one must never work in the shared
    checkout, so it gets a new worktree instead.
    """
    from pcode.app import _enter_worktree

    if Path(info.workspace).is_dir():
        return Path(info.workspace).resolve()
    print(f"workspace: {info.workspace} is gone; continuing in a new worktree", file=sys.stderr)
    created, _ = _enter_worktree(workspace, True, always=True, base=profile.base)
    return created.resolve()


def fork_worktree(saved, workspace: Path) -> Path:
    """A forked session's own worktree, started from the commit its original is on.

    Named after the copy's ID, as any session worktree is, so `pcode -c PREFIX`
    finds it. Outside git there is nothing to branch, so the copy shares the
    original's directory as `--fork` always has.
    """
    from pcode import worktree
    from pcode.app import _enter_worktree

    base = worktree.head_commit(workspace)
    if base is None:
        return workspace
    created, _ = _enter_worktree(workspace, saved.info.id[:8], always=True, base=base)
    created = created.resolve()
    saved.info.workspace = str(created)
    # Its own work starts here: /diffs and tidying on exit measure from it.
    saved.info.start_commit = base
    try:
        saved.save_info(touch=False)
    except BaseException:
        linked = worktree.describe(created)
        if linked is not None:
            worktree.remove(linked, force=True)
            worktree.delete_branch(linked, force=True)
        raise
    return created


async def _serve(args: argparse.Namespace) -> dict | None:
    """Run the host until it stops; `spawn_host` arguments for its successor, if any."""
    from pcode.app import _enter_worktree, _resume_workspace
    from pcode.preferences import (
        SETTINGS,
        load_preferences,
        set_project_root,
        thinking_mode_preference,
    )
    from pcode.project_trust import prompt_trust
    from pcode.sessions import SavedSession, first_prompt
    from pcode.worktree import leave_worktree

    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    profile = (
        remote_profile.RemoteProfile.from_json(args.remote_profile) if args.remote_profile else None
    )
    # Before anything reads it: extensions, the sandbox policy, the agent.
    remote_profile.activate(profile)
    workspace = args.workspace.resolve()
    entry = HostEntry(
        id=args.id,
        pid=os.getpid(),
        model=args.model,
        workspace=str(workspace),
        log=os.environ.get("PCODE_HOST_LOG", ""),
        code=code_fingerprint(),
        python=sys.executable,
    )
    write_entry(entry)
    set_project_root(workspace)
    # The terminal that started this host asked already; nobody is here to ask.
    prompt_trust(workspace, ask=None)
    saved = None
    session_id = None
    if args.resume:
        saved = SavedSession.open(
            args.resume, args.session_dir, workspace, fork_if_open=True, fork=args.fork
        )
        try:
            if profile is not None:
                workspace = remote_resume_workspace(saved.info, workspace, profile)
            else:
                workspace = Path(_resume_workspace(saved.info, workspace)).resolve()
                if args.fork and args.worktree:
                    workspace = fork_worktree(saved, workspace)
        except BaseException:
            saved.abandon()  # A copy made for this resume is removed, not left listed.
            raise
        entry.session_id = saved.info.id
        entry.forked_from = saved.info.forked_from or ""
        prompt = first_prompt(saved.info, saved.directory.parent)
        first = "" if prompt.startswith("(") else prompt
        entry.title = saved.info.name or saved.info.title or first
    elif profile is not None:
        # Never the shared checkout, even from inside a linked worktree.
        workspace, session_id = _enter_worktree(
            workspace, args.worktree or True, always=True, base=profile.base
        )
        workspace = workspace.resolve()
    elif not args.no_worktree:
        workspace, session_id = _enter_worktree(workspace, args.worktree)
        workspace = workspace.resolve()
    entry.workspace = str(workspace)
    write_entry(entry)

    host = SessionHost(entry)
    # An unattended session's worktree is left for its owner to inspect.
    host.keep_worktree = profile is not None
    controller = host.controller
    controller.model = args.model
    controller.workspace = workspace
    controller.session_dir = saved.directory.parent if saved is not None else args.session_dir
    controller.save_sessions = not args.no_save
    controller._saved_session = saved
    controller._session_id = session_id
    controller.resuming = saved is not None
    controller._needs_runtime = True
    controller.startup_pending = True
    controller.activity.thinking_mode = thinking_mode_preference()
    controller.register_skills()
    await host.serve()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, host.stop)
    print(f"serving {host.socket}", file=sys.stderr, flush=True)
    # Before boot adopts what an earlier pcode left running.
    host.own_jobs()
    await host.boot()
    watcher = None
    try:
        idle_minutes = _idle_minutes(load_preferences(), SETTINGS)
        host.active_at = time.monotonic()  # A slow boot is not idle time.
        if idle_minutes is not None:
            watcher = asyncio.create_task(host.stop_when_idle(idle_minutes))
        await host.stopped.wait()
    finally:
        if watcher is not None:
            watcher.cancel()
        from pcode.claude_sdk import shutdown

        runtime = controller.runtime
        try:
            try:
                await host.close()
            finally:
                from pcode import antigravity

                try:
                    await shutdown()
                finally:
                    await antigravity.shutdown()
        finally:
            # Lets go of the session, which a successor is about to open.
            if runtime is not None and hasattr(runtime, "close"):
                runtime.close()
            elif saved is not None:
                saved.close()  # Startup failed before a runtime took it.
        # A stop that came after `restart` must not tidy away the successor's worktree.
        if not host.keep_worktree and host.successor is None:
            # A terminal that asked to keep it tidies it itself, and can ask first.
            leave_worktree(
                controller.workspace,
                getattr(runtime, "session", None),
                ask=None,
                notify=lambda text: print(text, file=sys.stderr, flush=True),
                # Nobody chose to leave a session that stopped for being idle,
                # so its branch waits to be resumed rather than merged.
                merge=not host.idle_stopped,
            )
    if host.successor is None:
        return None
    return {
        "identity": host.successor,
        "model": controller.model,
        "workspace": controller.workspace,
        "resume": entry.session_id,
        "session_dir": controller.session_dir,
        "profile": profile,
    }


def start_successor(successor: dict) -> None:
    """Start the host a `restart` asked for, now this one has let go of the session.

    Spawned from here, it inherits this host's environment (a remote profile's
    is scrubbed already) and its remote profile.
    """
    from pcode.remote import spawn_host

    try:
        identity, _, log = spawn_host(**successor)
    except Exception as error:  # noqa: BLE001 - the log is the only reader.
        print(f"could not start the successor: {error_message(error)}", file=sys.stderr)
        return
    print(f"restarted as {identity} (log: {log})", file=sys.stderr, flush=True)


def _idle_minutes(preferences: dict, settings: dict) -> int | None:
    """`session_host_idle_minutes` as minutes; None for off. A bad value is the default."""
    key = "session_host_idle_minutes"
    for value in (preferences.get(key), settings[key].default):
        if value == "off":
            return None
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            return int(value)
    return 0


def main(argv: list[str] | None = None) -> None:
    import faulthandler

    args = _parser().parse_args(argv)
    # `kill -USR1 PID` writes every thread's stack to the log, for a host that hangs.
    faulthandler.register(signal.SIGUSR1)
    # The terminal that started the host may close; the host outlives it.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        successor = asyncio.run(_serve(args))
    except Exception as error:  # noqa: BLE001 - the log is the only reader.
        print(f"session host failed: {error_message(error)}", file=sys.stderr, flush=True)
        remove_entry(args.id)
        raise SystemExit(1) from error
    remove_entry(args.id)
    if successor is not None:
        start_successor(successor)


if __name__ == "__main__":
    main()
