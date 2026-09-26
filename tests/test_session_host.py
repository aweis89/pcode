"""Session hosts: the wire format, the host process's logic, and terminals attached to it.

Hosts run in-process here, over a real Unix socket. The spike behind this
feature ran them as separate processes; what that adds (environment
inheritance, detaching from the terminal's session) is `spawn_host`'s, covered
by its own test.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console

from pcode.agent import create_coder
from pcode.app import PreviewApp
from pcode.host import SessionHost
from pcode.host_protocol import (
    EVENT_TYPES,
    PROTOCOL,
    HostEntry,
    decode_event,
    encode_event,
    find_host,
    list_hosts,
    socket_path,
    write_entry,
)
from pcode.live import AgentRuntime
from pcode.remote import HostError, HostLaunch, RemoteRuntime, spawn_host
from pcode.runtime import (
    CacheBust,
    ChildPlan,
    ChildText,
    CommandOutput,
    EditCompleted,
    EditPreview,
    JobFinished,
    Message,
    PlanPreview,
    PlanUpdated,
    RunStatus,
    TextDelta,
    Thinking,
    ThinkingDelta,
    ToolStarted,
    ToolSummary,
)
from pcode.sessions import SavedSession
from pcode.ui import create_prompt

SAMPLES = [
    Message("**done**"),
    ToolStarted("shell", "ls", "c1", command="ls", arguments='{"command": "ls"}', purpose="look"),
    ToolSummary("shell", "ls", True, "c1", 1.5, "boom", "ls", result="out", outcome="failed"),
    JobFinished("shell", "sleep 9", call_id="j1", result="finished"),
    TextDelta("partial "),
    ThinkingDelta("hmm"),
    Thinking("hmm, done"),
    RunStatus("Thinking…"),
    CacheBust("cache collapsed"),
    PlanUpdated([{"content": "Inspect", "status": "in_progress"}]),
    PlanPreview(None),
    ChildPlan("c2", [{"content": "Child step", "status": "pending"}]),
    ChildText("c2", "child prose", thinking=True, start=True),
    CommandOutput("c3", "make test", "line 1\nline 2"),
    EditCompleted("c4", "a.py", "edit", "@@ -1 +1 @@\n-a\n+b", 1, 1),
    # Its own `kind` field once overwrote the event name on the wire.
    EditPreview("c5", "a.py", "print(1)", kind="code"),
]


def test_every_event_type_round_trips_through_json():
    assert {type(event).__name__ for event in SAMPLES} == set(EVENT_TYPES)
    for event in SAMPLES:
        assert decode_event(json.loads(json.dumps(encode_event(event)))) == event


def test_records_can_stop_where_a_running_turn_began(tmp_path):
    session = SavedSession.create("test", tmp_path, tmp_path / "sessions")
    try:
        session.append("turn_started", prompt="one", run_id="a", parent_id=None)
        session.append("Message", markdown="first answer", run_id="a")
        session.append("turn_completed", run_id="a")
        end = session.journal_size()
        session.append("turn_started", prompt="two", run_id="b", parent_id="a")
        session.append("TextDelta", text="streaming", run_id="b")
        assert [r["kind"] for r in session.records(end)] == [
            "turn_started",
            "Message",
            "turn_completed",
        ]
        settled = list(session.transcript_records(end))
        assert [r["kind"] for r in settled] == ["turn_started", "Message", "turn_completed"]
        # Without `end`, the running turn's streamed text shows as a partial.
        assert list(session.transcript_records())[-1] == {
            "kind": "partial",
            "markdown": "streaming",
        }
    finally:
        session.close()


@pytest.fixture
def host_dir(monkeypatch):
    # macOS caps a Unix socket path at 104 bytes, and pytest's tmp_path is longer.
    directory = Path(tempfile.mkdtemp(prefix="pch-", dir="/tmp"))
    monkeypatch.setenv("PCODE_HOST_DIR", str(directory))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def user_texts(messages) -> list[str]:
    return [
        str(part.content)
        for message in messages
        for part in getattr(message, "parts", [])
        if type(part).__name__ == "UserPromptPart" and not str(part.content).startswith("<")
    ]


class Script:
    """A model that answers by prompt; `hang ...` prompts wait on `release`."""

    def __init__(self):
        self.gates: dict[str, asyncio.Event] = {}
        self.requests: list[list] = []

    def gate(self, name: str) -> asyncio.Event:
        return self.gates.setdefault(name, asyncio.Event())

    def release(self, name: str) -> None:
        self.gate(name).set()

    async def model(self, messages, info):
        self.requests.append(messages)
        texts = user_texts(messages)
        prompt = texts[-1] if texts else ""
        if prompt == "read with steering":
            yield "Reading. "
            await self.gate(prompt).wait()
            yield {1: DeltaToolCall(name="read_file", json_args='{"path": "sample.txt"}')}
        elif "also check the tests" in texts:
            yield "Steering received."
        elif prompt.startswith("hang"):
            yield "Started. "
            await self.gate(prompt).wait()
            yield "Finished."
        else:
            yield f"Echo: {prompt}"


async def start_host(identity: str, workspace: Path, script: Script) -> SessionHost:
    (workspace / "sample.txt").write_text("a workspace marker\n")
    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=script.model), capabilities=[create_coder(workspace)]),
        session_factory=lambda model=None: SavedSession.create(
            "function:script", workspace, workspace / "sessions"
        ),
    )
    entry = HostEntry(
        id=identity, pid=os.getpid(), model="function:script", workspace=str(workspace)
    )
    host = SessionHost(runtime, entry)
    await host.serve()
    return host


async def stop_host(host: SessionHost) -> None:
    host.stop()
    await host.close()
    host.runtime.close()


async def collect(stream) -> list:
    return [event async for event in stream]


async def until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def text_of(events) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


def test_terminal_streams_a_turn_the_host_runs(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            terminal = await RemoteRuntime.connect(host.socket)
            events = await collect(terminal.stream("hello"))
            assert Message("Echo: hello") in events
            # The first turn creates the session; the host announces it.
            assert terminal.session_id == host.runtime.session.info.id
            (entry,) = list_hosts()
            assert (entry.id, entry.state, entry.title) == ("aaaa1111", "idle", "hello")
            assert entry.session_id == host.runtime.session.info.id
            assert find_host(entry.session_id[:8]).id == "aaaa1111"
            terminal.close()
        finally:
            await stop_host(host)
        assert list_hosts() == []

    asyncio.run(run())


def test_a_terminal_attaching_mid_turn_catches_up_exactly(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            first = await RemoteRuntime.connect(host.socket)
            await collect(first.stream("settled turn"))
            running = asyncio.create_task(collect(first.stream("hang here")))
            await until(lambda: host.buffer)
            late = await RemoteRuntime.connect(host.socket)
            # History is the journal up to the running turn; the turn itself
            # comes from the host's buffer, so nothing shows twice.
            prompts = [r["prompt"] for r in late.snapshot["records"] if r["kind"] == "turn_started"]
            assert prompts == ["settled turn"]
            assert late.pending_turn == {"prompt": "hang here", "echo": True}
            following = asyncio.create_task(collect(late.follow()))
            await asyncio.sleep(0.05)
            script.release("hang here")
            early, caught_up = await asyncio.gather(running, following)
            assert text_of(early) == text_of(caught_up) == "Started. Finished."
            first.close()
            late.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_cancel_stops_the_host_turn_and_the_next_prompt_is_not_lost(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            terminal = await RemoteRuntime.connect(host.socket)
            task = asyncio.create_task(collect(terminal.stream("hang forever")))
            await until(lambda: host.buffer)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # Sent while the cancelled turn is still unwinding in the host.
            reply = await asyncio.wait_for(collect(terminal.stream("next")), 5)
            assert [e.markdown for e in reply if isinstance(e, Message)] == ["Echo: next"]
            journal = (host.runtime.session.directory / "transcript.jsonl").read_text()
            assert '"turn_cancelled"' in journal
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_steering_reaches_the_hosts_next_model_request(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            terminal = await RemoteRuntime.connect(host.socket)
            turn = asyncio.create_task(collect(terminal.stream("read with steering")))
            await until(lambda: host.buffer)
            # The app hands over what was typed when it releases shell waits.
            terminal.take_steering = lambda: ["also check the tests"]
            terminal.release_waits()
            await until(lambda: host.steering)
            script.release("read with steering")
            events = await turn
            assert Message("Steering received.") in events
            assert "also check the tests" in user_texts(script.requests[-1])
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_steering_that_arrives_after_the_turn_starts_the_next_one(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            terminal = await RemoteRuntime.connect(host.socket)
            started = []
            terminal.on_turn = lambda prompt, echo: started.append((prompt, echo))
            turn = asyncio.create_task(collect(terminal.stream("hang briefly")))
            await until(lambda: host.buffer)
            terminal.take_steering = lambda: ["and then this"]
            terminal.release_waits()
            await until(lambda: host.steering)
            script.release("hang briefly")
            await turn
            await until(lambda: started)
            # This terminal showed the message when it sent it: no second echo.
            assert started == [("and then this", False)]
            followed = await collect(terminal.follow())
            assert Message("Echo: and then this") in followed
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_terminal_on_another_protocol_is_refused(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            with patch("pcode.remote.PROTOCOL", PROTOCOL + 1):
                with pytest.raises(HostError, match="protocol"):
                    await RemoteRuntime.connect(host.socket)
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_registry_forgets_hosts_whose_process_is_gone(host_dir):
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    write_entry(HostEntry(id="dead0000", pid=process.pid, model="m", workspace="/"))
    write_entry(HostEntry(id="live0000", pid=os.getpid(), model="m", workspace="/"))
    assert [entry.id for entry in list_hosts()] == ["live0000"]
    assert not (host_dir / "dead0000.json").exists()
    with pytest.raises(LookupError):
        find_host("nothing")


def test_socket_paths_that_are_too_long_fail_with_advice(tmp_path):
    with pytest.raises(OSError, match="PCODE_HOST_DIR"):
        socket_path("abcd1234", tmp_path / ("x" * 120))


def test_spawned_host_inherits_the_terminal_environment_and_its_own_session(host_dir, tmp_path):
    """The host is started from the terminal, so direnv credentials follow it there."""
    with patch("pcode.remote.subprocess.Popen") as popen:
        with patch.dict(os.environ, {"PCODE_SPAWN_CHECK": "from-this-terminal"}):
            identity, _, log = spawn_host(model="m", workspace=tmp_path, no_worktree=True)
    options = popen.call_args.kwargs
    assert options["env"]["PCODE_SPAWN_CHECK"] == "from-this-terminal"
    assert options["start_new_session"] is True
    assert options["cwd"] == tmp_path
    argv = popen.call_args.args[0]
    assert argv[:3] == [sys.executable, "-m", "pcode.host"]
    assert "--no-worktree" in argv and identity in argv
    assert log.parent == host_dir


def test_hosted_terminal_refuses_commands_that_need_a_local_runtime(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            output = StringIO()
            runtime = await RemoteRuntime.connect(host.socket)
            app = PreviewApp(
                model="function:script",
                runtime=runtime,
                console=Console(file=output, width=200),
                workspace=tmp_path,
            )
            assert app.hosted
            assert app.handle("/compact") is False
            assert "/compact is not available yet" in output.getvalue()
            rows = dict(app.session_overview())
            assert rows["Host"].startswith("aaaa1111")
            runtime.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_switch_leaves_a_running_turn_in_its_host_and_comes_back_to_it(tmp_path, host_dir):
    """The whole terminal: attach, switch away mid-turn, switch back, detach on quit."""

    async def run():
        script_a, script_b = Script(), Script()
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        host_a = await start_host("aaaa1111", tmp_path / "a", script_a)
        host_b = await start_host("bbbb2222", tmp_path / "b", script_b)
        output = StringIO()
        app = PreviewApp(
            model="function:script",
            workspace=tmp_path / "a",
            host=HostLaunch("aaaa1111"),
            console=Console(file=output, color_system=None, width=140),
        )
        try:
            with create_pipe_input() as pipe:
                session = None

                def prompt(*args, **kwargs):
                    nonlocal session
                    session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                    return session

                async def seen(text):
                    await until(lambda: text in output.getvalue(), timeout=10)

                async def drive():
                    await seen("Attached to session host aaaa1111")
                    pipe.send_text("hello a\r")
                    await seen("Echo: hello a")
                    pipe.send_text("hang in a\r")
                    await until(lambda: host_a.buffer)
                    pipe.send_text("/switch bbbb2222\r")
                    await seen("Switched to session bbbb2222")
                    assert app.runtime.id == "bbbb2222"
                    assert host_a.busy, "switching away must not stop A's turn"
                    await until(lambda: [e.id for e in app.hosts] == ["aaaa1111"])
                    pipe.send_text("hello b\r")
                    await seen("Echo: hello b")
                    pipe.send_text("/switch -\r")
                    await seen("Switched to session aaaa1111")
                    assert app.previous_host == "bbbb2222"
                    # Back mid-turn: the terminal follows A's turn again.
                    await until(lambda: app.activity.busy)
                    script_a.release("hang in a")
                    await seen("Finished.")
                    await until(lambda: not app.activity.busy)
                    pipe.send_text("/quit\r")

                with patch("pcode.app.create_prompt", prompt):
                    await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=30)
            assert "keeps running in the background" in output.getvalue()
            # Quitting detached: both hosts still serve, nobody attached.
            await until(lambda: not host_a.clients and not host_b.clients)
            assert not host_a.stopped.is_set() and not host_b.stopped.is_set()
        finally:
            await stop_host(host_a)
            await stop_host(host_b)

    asyncio.run(run())


def test_host_counts_turns_and_marks_ones_finished_unwatched_as_unseen(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            terminal = await RemoteRuntime.connect(host.socket)
            await until(lambda: list_hosts()[0].attached == 1)
            turn = asyncio.create_task(collect(terminal.stream("hang unwatched")))
            await until(lambda: host.buffer)
            terminal.detaching = True  # Leave without cancelling, as /switch does.
            turn.cancel()
            terminal.close()
            await until(lambda: list_hosts()[0].attached == 0)
            script.release("hang unwatched")
            await until(lambda: list_hosts()[0].turns == 1)
            (entry,) = list_hosts()
            assert (entry.outcome, entry.unseen, entry.state) == ("done", True, "idle")
            # Looking at it again is what clears it.
            again = await RemoteRuntime.connect(host.socket)
            await until(lambda: not list_hosts()[0].unseen)
            again.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_idle_host_stops_itself_and_a_busy_one_does_not(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            terminal = await RemoteRuntime.connect(host.socket)
            turn = asyncio.create_task(collect(terminal.stream("hang long")))
            await until(lambda: host.buffer)
            terminal.detaching = True
            turn.cancel()
            terminal.close()
            watcher = asyncio.create_task(host.stop_when_idle(0.0005, every=0.01))
            await asyncio.sleep(0.2)
            assert not host.stopped.is_set(), "a running turn is not idle"
            script.release("hang long")
            await asyncio.wait_for(host.stopped.wait(), 5)
            await watcher
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_stop_can_leave_the_worktree_for_the_terminal(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal = await RemoteRuntime.connect(host.socket)
            terminal.stop(keep_worktree=True)
            await asyncio.wait_for(host.stopped.wait(), 5)
            assert host.keep_worktree
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_turn_is_announced_on_the_desktop_once_however_many_terminals_see_it(host_dir):
    from pcode.host_protocol import claim

    assert claim("aaaa1111-3")
    assert not claim("aaaa1111-3")
    assert claim("aaaa1111-4")
    from pcode.host_protocol import remove_entry

    remove_entry("aaaa1111")
    assert not list(host_dir.glob("*.claim"))


def test_background_finish_notes_and_notifies_only_unwatched_sessions(tmp_path, host_dir):
    output = StringIO()
    app = PreviewApp(console=Console(file=output, width=200), workspace=tmp_path)
    sent = []
    app._emulator = sent.append
    watched = HostEntry("aaaa1111", 1, "m", "/", title="fix auth", turns=1, attached=1)
    unwatched = HostEntry("bbbb2222", 1, "m", "/", title="add tests", turns=2, outcome="failed")
    app.background_finished(watched)
    app.background_finished(unwatched)
    app.background_finished(unwatched)  # Another terminal, or a second look: no repeat.
    text = output.getvalue()
    assert "Background session aaaa1111 finished: fix auth" in text
    assert "Background session bbbb2222 failed: add tests" in text
    assert sent == ["\x1b]9;pcode: add tests — failed\x07"]


def test_notifications_cannot_break_out_of_their_escape(monkeypatch):
    from pcode.terminal_notify import notification, progress

    monkeypatch.delenv("TMUX", raising=False)
    assert notification("done\x07\x1b]52;c;evil\x07") == "\x1b]9;done  ]52;c;evil\x07"
    assert progress(True) == "\x1b]9;4;3\x07" and progress(False) == "\x1b]9;4;0\x07"
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    assert notification("hi") == "\x1bPtmux;\x1b\x1b]9;hi\x07\x1b\\"


def test_picker_puts_unseen_sessions_first_and_flags_old_code():
    from pcode.host_ui import host_row, ordered

    idle = HostEntry("idle0000", 1, "m", "/w/a", title="idle", updated=3)
    working = HostEntry("work0000", 1, "m", "/w/b", title="busy", state="working", updated=2)
    fresh = HostEntry(
        "new00000", 1, "m", "/w/c", title="done", state="idle", unseen=True, updated=1
    )
    assert [e.id for e in ordered([idle, working, fresh])] == ["new00000", "work0000", "idle0000"]
    old = HostEntry("old00000", 1, "m", "/w/d", title="old", state="idle", code="1")
    assert host_row(old, None, now=0, code="2").endswith("old code")
    assert "✓ new" in host_row(fresh, None, now=1, code="2")
    # No fingerprint at all: a host from before fingerprints, so older still.
    assert host_row(fresh, None, now=1, code="2").endswith("old code")
    current = HostEntry("cur00000", 1, "m", "/w/e", title="current", code="2")
    assert not host_row(current, None, now=1, code="2").endswith("old code")


def test_restart_stops_keeping_the_worktree_and_resumes_the_session(tmp_path):
    async def run():
        class Host:
            remote = True
            id, pid, session_id, lost = "aaaa1111", 4242, "session-1", False
            stopped_with = None

            def stop(self, *, keep_worktree=False):
                self.stopped_with = keep_worktree

        runtime = Host()
        app = PreviewApp(model="m", runtime=runtime, console=Console(file=StringIO()))
        started = []

        async def start_host_session(prompt="", *, resume=None, note=None):
            started.append((prompt, resume, note))

        app.start_host_session = start_host_session
        app.restart("")
        # An async function patches as an AsyncMock, awaitable as it is.
        with patch("pcode.remote.wait_for_exit") as waited:
            await app.restart_host()
        waited.assert_awaited_once_with(4242)
        assert runtime.stopped_with is True
        assert started == [("", "session-1", "Restarted on the current pcode")]

    asyncio.run(run())
