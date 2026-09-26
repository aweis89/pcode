"""A headless process that owns one conversation, for terminals to attach to.

The host runs `AgentRuntime` exactly as a terminal would, but renders nothing:
every event goes to the attached terminals (see `pcode.host_protocol` for the
wire format), and a copy of the running turn is kept so a terminal attaching
mid-turn can catch up. Terminals come and go; the host stops only when told to.

Started by `pcode.remote.spawn_host`, which runs `python -m pcode.host` from the
terminal that asked for it, so the host inherits that terminal's environment
(direnv credentials, `PATH`) the way a local session would.
"""

import argparse
import asyncio
import os
import signal
import sys
from contextlib import aclosing
from pathlib import Path

from pcode.host_protocol import (
    LINE_LIMIT,
    PROTOCOL,
    HostEntry,
    dumps,
    encode_event,
    host_dir,
    read_message,
    remove_entry,
    socket_path,
    write_entry,
)

# Consecutive snapshots of one of these replace each other in the catch-up
# buffer: each carries the whole state, so only the newest matters.
SNAPSHOT_KINDS = {"CommandOutput", "EditPreview", "ChildPlan", "PlanPreview"}
DELTA_KINDS = {"TextDelta", "ThinkingDelta"}


class _Client:
    """One attached terminal. Writes go through a queue so a slow reader never stalls the turn."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.outbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.task = asyncio.create_task(self._drain())

    def send(self, message: dict) -> None:
        self.outbox.put_nowait(dumps(message))

    def close(self) -> None:
        self.outbox.put_nowait(None)

    async def _drain(self) -> None:
        try:
            while (data := await self.outbox.get()) is not None:
                self.writer.write(data)
                await self.writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self.writer.close()


class SessionHost:
    def __init__(self, runtime, entry: HostEntry, directory: Path | None = None) -> None:
        self.runtime = runtime
        self.entry = entry
        self.directory = directory or host_dir()
        self.clients: set[_Client] = set()
        self.turn: asyncio.Task | None = None
        self.turn_prompt: str | None = None
        self.turn_source = ""
        self.turn_request = ""
        # Prompts sent while a turn runs, and steering the turn never took.
        self.queue: list[tuple[str, str, str]] = []
        self.steering: list[str] = []
        # The running turn so far, for terminals that attach in the middle of it.
        self.buffer: list[dict] = []
        # Journal bytes that were settled before the running turn started.
        self.settled_end = 0
        self.stopped = asyncio.Event()
        self.server: asyncio.AbstractServer | None = None
        runtime.take_steering = self._take_steering
        # The level tells the terminal which of its own notice handlers to use.
        runtime.compaction_notice = lambda text: self.notice(text, "compaction")
        runtime.retry_notice = lambda text: self.notice(text, "retry")
        runtime.warning_notice = lambda text: self.notice(text, "warning")

    @property
    def busy(self) -> bool:
        return self.turn is not None

    @property
    def socket(self) -> Path:
        return socket_path(self.entry.id, self.directory)

    async def serve(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.socket
        path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._accept, path=path, limit=LINE_LIMIT)
        os.chmod(path, 0o600)
        self.update(state="idle")

    def update(self, **changes) -> None:
        for key, value in changes.items():
            setattr(self.entry, key, value)
        session = getattr(self.runtime, "session", None)
        if session is not None and session.info.id != self.entry.session_id:
            self.entry.session_id = session.info.id
            self.broadcast({"type": "session", "session_id": session.info.id})
        write_entry(self.entry, self.directory)

    def context(self) -> str:
        """The footer's context usage, which the terminal cannot work out without history."""
        from pcode.context_usage import context_label

        agent = getattr(self.runtime, "agent", None)
        history = getattr(self.runtime, "context_history", None)
        if history is None:
            history = getattr(self.runtime, "history", ())
        try:
            return context_label(getattr(agent, "model", None) or self.entry.model, history)
        except Exception:  # noqa: BLE001 - a footer label must not break a turn.
            return ""

    # Terminal connections

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(writer)
        try:
            hello = await read_message(reader)
            if hello is None or hello.get("type") != "hello":
                return
            if hello.get("protocol") != PROTOCOL:
                client.send(
                    {
                        "type": "error",
                        "message": f"This session host speaks protocol {PROTOCOL}, the terminal "
                        f"{hello.get('protocol')}. Restart one of them on the same pcode.",
                    }
                )
                return
            # Snapshot and registration happen with no await between them, so
            # the terminal sees every event exactly once: in the snapshot or after it.
            client.send(self.welcome())
            client.send(self.snapshot())
            self.clients.add(client)
            while (message := await read_message(reader)) is not None:
                self.handle(message)
        except (ValueError, ConnectionError):
            pass
        finally:
            self.clients.discard(client)
            client.close()

    def welcome(self) -> dict:
        from pcode.preferences import current_effort

        startup = getattr(self.runtime, "startup_context", None)
        return {
            "effort": current_effort(getattr(self.runtime, "agent", None), self.entry.model),
            "type": "welcome",
            "protocol": PROTOCOL,
            "id": self.entry.id,
            "pid": os.getpid(),
            "model": self.entry.model,
            "workspace": self.entry.workspace,
            "session_id": self.entry.session_id,
            "busy": self.busy,
            "context": self.context(),
            "startup_context": startup() if startup is not None else [],
        }

    def snapshot(self) -> dict:
        """Settled history from the journal, plus the running turn from memory.

        The journal holds a running turn's settled steps too, but not its
        streaming text, previews, or live command output, so a running turn is
        cut out of the journal read and sent from the buffer instead.
        """
        session = getattr(self.runtime, "session", None)
        records, plan = [], []
        if session is not None:
            end = self.settled_end if self.busy else None
            records = list(session.transcript_records(end))
            plan = session.latest_plan()
        turn = None
        if self.busy:
            turn = {
                "prompt": self.turn_prompt,
                "source": self.turn_source,
                "request": self.turn_request,
                "messages": list(self.buffer),
            }
        return {
            "type": "snapshot",
            "session_id": session.info.id if session is not None else "",
            "forked_from": getattr(session, "forked_from", None) or "",
            "records": records,
            "plan": plan,
            "turn": turn,
        }

    def broadcast(self, message: dict) -> None:
        for client in list(self.clients):
            client.send(message)

    def handle(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "prompt":
            text = str(message.get("text", ""))
            request = str(message.get("request", ""))
            if self.busy or self.queue:
                self.queue.append((text, "user", request))
            else:
                self.start_turn(text, "user", request)
        elif kind == "steer":
            texts = [str(text) for text in message.get("messages", []) if text]
            if not texts:
                return
            if self.busy:
                self.steering.extend(texts)
                jobs = getattr(self.runtime, "jobs", None)
                if jobs is not None:
                    # Delivered at the next model request; a shell wait is what
                    # stands between now and that request.
                    jobs.release_waits()
            else:
                self.start_turn("\n\n".join(texts), "steering")
        elif kind == "cancel":
            self.cancel(str(message.get("policy") or "stop"))
        elif kind == "stop":
            self.stop()

    # Turns

    def start_turn(self, prompt: str, source: str, request: str = "") -> None:
        session = getattr(self.runtime, "session", None)
        self.settled_end = session.journal_size() if session is not None else 0
        self.turn_prompt, self.turn_source, self.turn_request = prompt, source, request
        self.buffer = []
        self.update(state="working", last_prompt=prompt, title=self.entry.title or prompt)
        self.broadcast(
            {"type": "turn_started", "prompt": prompt, "source": source, "request": request}
        )
        self.turn = asyncio.create_task(self._run(prompt))

    async def _run(self, prompt: str) -> None:
        from pcode.live import error_message

        outcome, error = "done", ""
        try:
            async with aclosing(self.runtime.stream(prompt)) as stream:
                async for event in stream:
                    self.emit({"type": "event", "event": encode_event(event)})
                    if not self.entry.session_id and getattr(self.runtime, "session", None):
                        self.update()  # The first turn creates the session.
        except asyncio.CancelledError:
            outcome = "cancelled"
        except Exception as failure:  # noqa: BLE001 - reported to the terminals.
            outcome, error = "failed", error_message(failure)
        finally:
            jobs = getattr(self.runtime, "jobs", None)
            if jobs is not None:
                jobs.cancel_policy = "detach"
        session = getattr(self.runtime, "session", None)
        self.turn = None
        self.turn_prompt = None
        self.buffer = []
        self.broadcast(
            {
                "type": "turn_finished",
                "outcome": outcome,
                "error": error,
                "request": self.turn_request,
                "session_dir": str(session.directory) if session is not None else "",
                "recovery_blocked": getattr(self.runtime, "recovery_blocked", None) or "",
                "context": self.context(),
            }
        )
        self.update(state="idle")
        if outcome == "failed":
            # As a local terminal drops its queue when a turn fails.
            self.queue.clear()
            self.steering.clear()
        # Not on "cancelled": `cancel` cleared the queue when it was asked, so
        # what is queued now was sent after it, while the turn was unwinding.
        self._next()

    def _next(self) -> None:
        if self.stopped.is_set():
            return
        if self.steering:
            # Steering the turn ended before taking starts the next one, as a
            # message queued behind a local turn would.
            texts, self.steering = self.steering, []
            self.start_turn("\n\n".join(texts), "steering")
        elif self.queue:
            self.start_turn(*self.queue.pop(0))

    def _take_steering(self) -> list[str]:
        texts, self.steering = self.steering, []
        if texts:
            self.emit({"type": "steering_taken", "messages": texts})
        return texts

    def cancel(self, policy: str = "stop") -> None:
        self.queue.clear()
        self.steering.clear()
        if self.turn is None or self.turn.done():
            return
        jobs = getattr(self.runtime, "jobs", None)
        if jobs is not None:
            jobs.cancel_policy = policy
        self.turn.cancel()

    def emit(self, message: dict) -> None:
        """Send to every terminal and keep for the ones that attach later."""
        self.broadcast(message)
        if self.turn is None:
            return
        previous = self.buffer[-1] if self.buffer else None
        event = message.get("event")
        if previous is not None and event is not None and previous.get("type") == "event":
            before = previous["event"]
            kind = event["kind"]
            if kind == before["kind"] and kind in DELTA_KINDS:
                text = before["fields"]["text"] + event["fields"]["text"]
                self.buffer[-1] = {
                    **previous,
                    "event": {"kind": kind, "fields": {**before["fields"], "text": text}},
                }
                return
            if (
                kind == before["kind"]
                and kind in SNAPSHOT_KINDS
                and event["fields"].get("call_id") == before["fields"].get("call_id")
            ):
                self.buffer[-1] = message
                return
        self.buffer.append(message)

    def notice(self, text: str, level: str = "note") -> None:
        self.emit({"type": "notice", "level": level, "text": str(text)})

    def stop(self) -> None:
        self.cancel()
        self.stopped.set()

    async def close(self) -> None:
        if self.turn is not None:
            self.turn.cancel()
            await asyncio.gather(self.turn, return_exceptions=True)
        self.broadcast({"type": "closed"})
        for client in list(self.clients):
            client.close()
        await asyncio.gather(*(client.task for client in self.clients), return_exceptions=True)
        if self.server is not None:
            self.server.close()
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
    from pcode.agent import create_agent
    from pcode.app import _enter_worktree, _resume_workspace, leave_worktree
    from pcode.ext import ExtensionUI, load_extensions
    from pcode.live import AgentRuntime
    from pcode.preferences import (
        apply_effort,
        apply_thinking,
        effort_for,
        load_preferences,
        set_project_root,
    )
    from pcode.project_trust import prompt_trust
    from pcode.sessions import SavedSession, first_prompt

    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    workspace = args.workspace.resolve()
    entry = HostEntry(
        id=args.id,
        pid=os.getpid(),
        model=args.model,
        workspace=str(workspace),
        log=os.environ.get("PCODE_HOST_LOG", ""),
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
    host: SessionHost | None = None

    def notify(text: str, level: str = "info") -> None:
        if host is not None:
            host.notice(text, "warning" if level in ("warning", "error") else "note")
        print(f"{level}: {text}", file=sys.stderr, flush=True)

    def request_reload() -> None:
        raise ValueError("Extension reload is not available in a session host yet.")

    extensions = await asyncio.to_thread(
        load_extensions,
        workspace,
        ExtensionUI(notify, request_reload),
        session_dir=args.session_dir,
    )
    agent = create_agent(args.model, workspace, extensions.capabilities, extensions.subagents)
    apply_effort(agent, args.model, effort_for(args.model))
    apply_thinking(agent, args.model, load_preferences().get("show_thinking") == "on")

    def create_session(model: str | None = None):
        nonlocal session_id
        identity, session_id = session_id, None
        return SavedSession.create(
            model or args.model, workspace, args.session_dir, identity=identity
        )

    runtime = AgentRuntime(agent, saved, session_factory=None if args.no_save else create_session)
    if saved is not None:
        await runtime.restore()
    try:
        await runtime.refresh_context()
    except Exception as error:  # noqa: BLE001 - metadata only.
        print(f"context metadata unavailable: {error}", file=sys.stderr, flush=True)
    host = SessionHost(runtime, entry)
    await host.serve()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, host.stop)
    await _enable_default_mcp(runtime, host)
    print(f"serving {host.socket}", file=sys.stderr, flush=True)
    try:
        await host.stopped.wait()
    finally:
        await host.close()
        runtime.close()
        await extensions.close()
        leave_worktree(
            workspace,
            runtime.session,
            ask=None,
            notify=lambda text: print(text, file=sys.stderr, flush=True),
        )
        if saved is not None:
            saved.close()


async def _enable_default_mcp(runtime, host: SessionHost) -> None:
    """Servers marked enabled in mcp.json, with saved sign-ins only: nobody can open a browser."""
    from pcode.mcp import default_servers

    state = getattr(runtime, "mcp", None)
    if state is None:
        return
    try:
        names = default_servers()
    except ValueError as error:
        host.notice(str(error), "warning")
        return
    for name in names:
        try:
            await state.enable(name, interactive=False)
        except Exception as error:  # noqa: BLE001 - one server must not stop the host.
            host.notice(f"MCP '{name}' not enabled: {error}", "warning")


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
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
