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

from pcode.controller import INTENTS, SESSION_FIELDS, SessionController
from pcode.host_protocol import (
    LINE_LIMIT,
    PROTOCOL,
    HostEntry,
    code_fingerprint,
    dumps,
    host_dir,
    read_message,
    remove_entry,
    socket_path,
    write_entry,
)
from pcode.rpc import Peer
from pcode.runtime import CommandOutput, EditPreview, TextDelta, ThinkingDelta
from pcode.ui import Activity

# Consecutive events of these kinds merge in the catch-up buffer: deltas join,
# and a newer snapshot of the same call replaces the older one.
DELTA_EVENTS = (TextDelta, ThinkingDelta)
SNAPSHOT_EVENTS = (CommandOutput, EditPreview)

# View calls that change what the conversation is, so a terminal attaching
# later reads it from the journal instead of from the buffer.
RESETS = {"conversation_reset", "replay_conversation", "show_branch"}

# What a terminal may call besides the controller's intents.
HOST_CALLS = frozenset({"attach", "query", "send", "run", "stop", "asides_read"})

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


class _Client:
    """One attached terminal, and the calls it may make."""

    def __init__(self, host: "SessionHost", number: int) -> None:
        self.host = host
        self.number = number
        self.peer: Peer | None = None
        # A caller with no editor (`--attach --print`): popups and side-answer
        # notices meant for "the terminal last used" skip it.
        self.headless = False

    # Intents: the controller's, with commands tagged by who sent them.

    def submit(self, text: str, mode: str) -> None:
        self.host.touch(self)
        self.host.controller.submit(text, mode)

    def command(self, text: str, tag=None) -> None:
        self.host.touch(self)
        self.host.controller.command(text, (self.number, tag))

    # Host calls for a caller with no editor to watch

    def send(self, text: str) -> None:
        """Queue `text` as a turn of its own, and tell the caller how to know that turn.

        The caller knows its turn by its text, so it skips as many identical
        ones as were queued ahead. `queued` goes out in order with the view
        calls: every `turn_started` before it is for a turn taken earlier, and
        the reply to this call can arrive after the turn has begun.
        """
        self.headless = True
        self.host.active_at = time.monotonic()
        ahead = self.host.activity.queued_prompts.count(text)
        self.host.controller.submit(text, "queue")
        self.peer.notify("queued", text, ahead)

    async def run(self, text: str) -> bool:
        """`command`, returning once it has run, and whether it did.

        A command dropped from the queue (a Ctrl+C elsewhere cleared it) or
        refused (one that waits for an idle session) did not. Background work
        the command started (compaction, MCP sign-in) is waited for, as its
        outcome is the point of sending, say, `/compact`.
        """
        self.headless = True
        tag = (self.number, f"run-{next(self.host.run_ids)}")
        run = self.host.runs[tag] = _Run()
        try:
            self.command(text, tag[1])
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

    def cancel(self) -> None:
        self.host.controller.cancel()

    def set_thinking(self, shown: bool) -> None:
        self.host.controller.set_thinking(shown)

    def adjust_effort(self, direction: int) -> None:
        self.host.controller.adjust_effort(direction)
        self.host.controller.view.session_changed()

    def stop_jobs(self, job_ids) -> None:
        self.host.controller.stop_jobs(job_ids)

    def watch_job(self, job_id) -> None:
        self.host.controller.watch_job(job_id)

    # Host calls

    def attach(self) -> dict:
        """Everything needed to show the session now; later changes follow as view calls.

        Built and registered with no await in between, so every change arrives
        exactly once: in this welcome or after it.
        """
        return self.host.attach(self)

    async def query(self, name: str, *args):
        return await self.host.controller.query(name, *args)

    def asides_read(self, identities) -> None:
        """The terminal showed these answers; no terminal needs telling they are ready."""
        for aside in self.host.controller.asides.items:
            if aside.id in identities and not aside.read:
                aside.read = True
                self.host.emit_aside(aside)

    def stop(self, keep_worktree: bool = False) -> None:
        self.host.keep_worktree = bool(keep_worktree)
        self.host.stop()


class HostView:
    """The controller's view in a host: calls go to the attached terminals.

    Plain calls go to every terminal and into the catch-up buffer. Popups and
    commands handed back go to the terminal whose command is running (a
    command's tag carries its sender), or else the one that sent the last
    thing; with no terminal attached they are answered with nothing.
    """

    def __init__(self, host: "SessionHost") -> None:
        self._host = host

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
        if (run := self._host.run_for(tag)) and not run.done.done():
            run.done.set_result(None)
        if not self._host.controller.running:
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
        if client is not None:
            client.peer.notify("command_started", own_tag)

    def command_finished(self) -> None:
        client, self._host.running_command = self._host.running_command, None
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
        return await self._ask("read_asides")

    async def browse_jobs(self) -> None:
        await self._ask("browse_jobs")


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

    async def boot(self) -> None:
        """Build the runtime, say what a new session says, then start the loops.

        Terminals may attach while this runs: they see the startup notes as
        they come, and anything they send waits for the loops.
        """
        from pcode.live import error_message

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
            await controller.warn_meridian_thinking()
            # Optional, and it can hang; the session must not wait for it.
            self.tasks.append(asyncio.create_task(self._refresh_context()))
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
        try:
            hello = await read_message(reader)
        except ValueError:
            hello = None
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
        except (ValueError, ConnectionError):
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
                    self.update(attached=len(self.clients))

    def attach(self, client: _Client) -> dict:
        from pcode.rpc import encode

        self.clients[client.number] = client
        self._latest = client
        # Shown now, so whatever finished while nobody watched has been seen.
        self.update(attached=len(self.clients), unseen=False)
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
            # Already encoded: a buffered call's arguments are whatever the view got.
            "calls": [
                [name, encode(list(args)), encode(kwargs)] for name, args, kwargs in self.buffer
            ],
        }

    def touch(self, client: _Client) -> None:
        self._latest = client
        self.active_at = time.monotonic()

    def latest_client(self) -> _Client | None:
        """The terminal last used, for what goes to one terminal; never a headless caller."""
        latest = self._latest
        if latest is not None and latest.peer and not latest.headless:
            return latest
        # Attach order: the newest terminal that has an editor.
        return next((c for c in reversed(self.clients.values()) if not c.headless), None)

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
            "replied": aside.reply is not None,
            "read": aside.read,
        }

    def emit_aside(self, aside) -> None:
        for client in list(self.clients.values()):
            client.peer.notify("aside_changed", self.aside_state(aside))

    def push_state(self) -> None:
        """Send the session state if it changed since the last one sent."""
        state = self.controller.session_state()
        if state != self._state:
            self._state = state
            for client in list(self.clients.values()):
                client.peer.notify("session_state", state)
        session = getattr(self.controller.runtime, "session", None)
        if session is not None and session.info.id != self.entry.session_id:
            self.update(session_id=session.info.id)

    # The entry other terminals read

    def update(self, **changes) -> None:
        for key, value in changes.items():
            setattr(self.entry, key, value)
        write_entry(self.entry, self.directory)

    def turn_began(self, prompt: str) -> None:
        self.reset_buffer()
        self.update(state="working", last_prompt=prompt, title=self.entry.title or prompt)

    def turn_finished(self) -> None:
        outcome = {"done": "done", "failed": "failed", "cancelled": "cancelled"}.get(
            self.activity.prompt_state, "done"
        )
        self.active_at = time.monotonic()
        self.push_state()
        self.update(
            state="idle",
            outcome=outcome,
            turns=self.entry.turns + 1,
            unseen=not self.clients,
        )

    # Stopping

    def idle(self) -> bool:
        """Nothing would be lost by stopping: no terminal, no work, no running job."""
        jobs = getattr(self.controller.runtime, "jobs", None)
        running = jobs.running() if jobs is not None else []
        return not self.clients and not self.busy and not running

    async def stop_when_idle(self, minutes: float, *, every: float = 30.0) -> None:
        """Stop after `minutes` idle. The journal keeps the conversation for /resume."""
        while not self.stopped.is_set():
            await asyncio.sleep(every)
            if not self.idle():
                self.active_at = time.monotonic()
            elif time.monotonic() - self.active_at >= minutes * 60:
                print(f"idle for {minutes:g} min; stopping", file=sys.stderr, flush=True)
                self.stop()

    def stop(self) -> None:
        self.stopped.set()

    async def close(self) -> None:
        controller = self.controller
        controller.running = False
        # Terminals hear that the host is going, not the teardown after it
        # (a "Run cancelled" for a host stopped while idle).
        clients, self.clients = list(self.clients.values()), {}
        for client in clients:
            client.peer.notify("host_closed")
            client.peer.close()
        controller.cancel()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, *controller.tasks(), return_exceptions=True)
        await controller.asides.close()
        if self.server is not None:
            self.server.close()
        if controller.extensions is not None:
            await controller.extensions.close()
        remove_entry(self.entry.id, self.directory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one pcode conversation headless.")
    parser.add_argument("--id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--resume")
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--worktree", nargs="?", const=True)
    parser.add_argument("--no-worktree", action="store_true")
    return parser


async def _serve(args: argparse.Namespace) -> None:
    from pcode.app import _enter_worktree, _resume_workspace
    from pcode.preferences import load_preferences, set_project_root
    from pcode.project_trust import prompt_trust
    from pcode.sessions import SavedSession, first_prompt
    from pcode.worktree import leave_worktree

    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    workspace = args.workspace.resolve()
    entry = HostEntry(
        id=args.id,
        pid=os.getpid(),
        model=args.model,
        workspace=str(workspace),
        log=os.environ.get("PCODE_HOST_LOG", ""),
        code=code_fingerprint(),
    )
    write_entry(entry)
    set_project_root(workspace)
    # The terminal that started this host asked already; nobody is here to ask.
    prompt_trust(workspace, ask=None)
    saved = None
    session_id = None
    if args.resume:
        saved = SavedSession.open(args.resume, args.session_dir, workspace, fork_if_open=True)
        workspace = Path(_resume_workspace(saved.info, workspace)).resolve()
        entry.session_id = saved.info.id
        prompt = first_prompt(saved.info, saved.directory.parent)
        entry.title = "" if prompt.startswith("(") else prompt
    elif not args.no_worktree:
        workspace, session_id = _enter_worktree(workspace, args.worktree)
        workspace = workspace.resolve()
    entry.workspace = str(workspace)
    write_entry(entry)

    host = SessionHost(entry)
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
    controller.activity.show_thinking = load_preferences().get("show_thinking") == "on"
    controller.register_skills()
    await host.serve()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, host.stop)
    print(f"serving {host.socket}", file=sys.stderr, flush=True)
    await host.boot()
    idle_minutes = int(load_preferences().get("session_host_idle_minutes", "60") or 0)
    watcher = asyncio.create_task(host.stop_when_idle(idle_minutes)) if idle_minutes else None
    try:
        await host.stopped.wait()
    finally:
        if watcher is not None:
            watcher.cancel()
        await host.close()
        runtime = controller.runtime
        if runtime is not None and hasattr(runtime, "close"):
            runtime.close()
        if not host.keep_worktree:
            # A terminal that asked to keep it tidies it itself, and can ask first.
            leave_worktree(
                controller.workspace,
                getattr(runtime, "session", None),
                ask=None,
                notify=lambda text: print(text, file=sys.stderr, flush=True),
            )


def main(argv: list[str] | None = None) -> None:
    import faulthandler

    args = _parser().parse_args(argv)
    # `kill -USR1 PID` writes every thread's stack to the log, for a host that hangs.
    faulthandler.register(signal.SIGUSR1)
    # The terminal that started the host may close; the host outlives it.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        asyncio.run(_serve(args))
    except Exception as error:  # noqa: BLE001 - the log is the only reader.
        from pcode.live import error_message

        print(f"session host failed: {error_message(error)}", file=sys.stderr, flush=True)
        remove_entry(args.id)
        raise SystemExit(1) from error
    remove_entry(args.id)


if __name__ == "__main__":
    main()
