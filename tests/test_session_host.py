"""Session hosts: the wire format, the host process's logic, and terminals attached to it.

Hosts run in-process here, over a real Unix socket: a `SessionHost` serving a
`SessionController`, and terminals that are either a `RemoteController` with a
recording view or a whole `PreviewApp`. What a separate process adds
(environment inheritance, detaching from the terminal's session) is
`spawn_host`'s, covered by its own test.
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
from pcode.remote import HostError, HostLaunch, RemoteController, spawn_host
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
from pcode.ui import Activity, create_prompt

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
    """A host serving a controller over a scripted model, as `pcode.host` sets one up."""
    (workspace / "sample.txt").write_text("a workspace marker\n")
    entry = HostEntry(
        id=identity, pid=os.getpid(), model="function:script", workspace=str(workspace)
    )
    host = SessionHost(entry)
    controller = host.controller
    controller.model = "function:script"
    controller.workspace = workspace
    controller.runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=script.model), capabilities=[create_coder(workspace)]),
        session_factory=lambda model=None: SavedSession.create(
            "function:script", workspace, workspace / "sessions"
        ),
    )
    await host.serve()
    host.reset_buffer()
    host.push_state()
    host.start()
    return host


async def stop_host(host: SessionHost) -> None:
    host.stop()
    await host.close()
    host.controller.runtime.close()


class View:
    """A terminal's view that records every call the host makes to it."""

    ASYNC = {"after_turn", "after_command", "run_command", "browse_jobs", "choose_model"}

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.answers = {"choose_model": None, "read_asides": None}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name in self.ASYNC or name in self.answers:
                return self._answer(name)
            return None

        return call

    async def _answer(self, name):
        return self.answers.get(name)

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def count(self, name: str) -> int:
        return self.names().count(name)

    def events(self) -> list:
        return [args[0] for name, args, _ in self.calls if name == "turn_event"]


async def attach(host: SessionHost) -> tuple[RemoteController, View, dict]:
    view = View()
    controller, welcome = await RemoteController.connect(host.socket, view, Activity())
    await controller.start(welcome)
    return controller, view, welcome


async def until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def text_of(events) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


def test_terminal_sees_a_turn_the_host_runs(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, view, _ = await attach(host)
            terminal.submit("hello", "queue")
            await until(lambda: view.count("after_turn"))
            assert Message("Echo: hello") in view.events()
            assert view.names()[:1] == ["state"] or "turn_started" in view.names()
            # The first turn creates the session; the host says so.
            saved = host.controller.runtime.session
            await until(lambda: terminal.runtime.session_id == saved.info.id)
            assert terminal.runtime.session.directory == saved.directory
            (entry,) = list_hosts()
            assert (entry.id, entry.state, entry.title) == ("aaaa1111", "idle", "hello")
            assert entry.session_id == saved.info.id
            assert find_host(entry.session_id[:8]).id == "aaaa1111"
            # The live panel's session fields are mirrored as they change.
            assert not terminal.activity.busy and terminal.activity.prompt_state == "done"
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
            first, first_view, _ = await attach(host)
            first.submit("settled turn", "queue")
            await until(lambda: first_view.count("after_turn") == 1)
            first.submit("hang here", "queue")
            await until(lambda: any(name == "turn_event" for name, _, _ in host.buffer))
            view = View()
            late, welcome = await RemoteController.connect(host.socket, view, Activity())
            # History is the journal up to the running turn; the turn itself
            # comes from the host's buffer, so nothing shows twice.
            journal = late.runtime.session
            records = journal.transcript_records(welcome["settled_end"])
            prompts = [record["prompt"] for record in records if record["kind"] == "turn_started"]
            assert prompts == ["settled turn"]
            assert welcome["calls"][0][:2] == ["turn_started", ["hang here"]]
            assert late.activity.busy and late.activity.prompt == "hang here"
            await late.start(welcome)
            script.release("hang here")
            await until(lambda: view.count("after_turn") and first_view.count("after_turn") == 2)
            second_turn = first_view.events()[
                first_view.events().index(Message("Echo: settled turn")) + 1 :
            ]
            assert text_of(second_turn) == text_of(view.events()) == "Started. Finished."
            first.close()
            late.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_cancel_stops_the_host_turn_and_the_next_prompt_is_not_lost(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            terminal, view, _ = await attach(host)
            terminal.submit("hang forever", "queue")
            await until(lambda: host.buffer)
            terminal.cancel()
            # Sent while the cancelled turn is still unwinding in the host.
            terminal.submit("next", "queue")
            await until(lambda: Message("Echo: next") in view.events())
            assert "cancelled" in view.names()
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_steering_reaches_the_hosts_next_model_request(tmp_path, host_dir):
    async def run():
        script = Script()
        host = await start_host("aaaa1111", tmp_path, script)
        try:
            terminal, view, _ = await attach(host)
            terminal.submit("read with steering", "queue")
            await until(lambda: host.buffer)
            terminal.submit("also check the tests", "steering")
            await until(lambda: terminal.activity.queued_modes == ["steering"])
            script.release("read with steering")
            await until(lambda: Message("Steering received.") in view.events())
            assert "also check the tests" in user_texts(script.requests[-1])
            # Shown when the model took it, like a local session's steering.
            assert ("user", ("also check the tests",), {}) in view.calls
            terminal.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_session_commands_run_in_the_host_and_ask_the_terminal_that_sent_them(
    tmp_path, host_dir, monkeypatch
):
    async def run():
        monkeypatch.setattr("pcode.models.active_providers", lambda model: {"anthropic"})
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            sender, view, _ = await attach(host)
            watcher, other, _ = await attach(host)
            sender.command("/model", 7)
            await until(lambda: view.count("choose_model"))
            # The picker opens only where the command was typed, with its tag.
            assert ("command_started", (7,), {}) in view.calls
            assert not other.count("choose_model")
            await until(lambda: view.count("command_finished"))
            sender.command("/effort")
            await until(lambda: view.count("flash") and other.count("flash"))
            # A command the terminal owns comes back to it, in order with the rest.
            sender.command("/theme dark", 8)
            await until(lambda: view.count("run_command"))
            assert ("run_command", ("/theme dark",), {"idle": True, "tag": 8}) in view.calls
            assert not other.count("run_command")
            assert "/effort" in [command.name for command in sender.registry.commands]
            sender.close()
            watcher.close()
        finally:
            await stop_host(host)

    asyncio.run(run())


def test_a_terminal_on_another_protocol_is_refused(tmp_path, host_dir):
    async def run():
        host = await start_host("aaaa1111", tmp_path, Script())
        try:
            with patch("pcode.remote.PROTOCOL", PROTOCOL + 1):
                with pytest.raises(HostError, match="protocol"):
                    await RemoteController.connect(host.socket, View(), Activity())
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
                    # Back mid-turn: the terminal shows A's turn again.
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
            terminal, _, _ = await attach(host)
            await until(lambda: list_hosts()[0].attached == 1)
            terminal.submit("hang unwatched", "queue")
            await until(lambda: host.buffer)
            terminal.close()
            await until(lambda: list_hosts()[0].attached == 0)
            script.release("hang unwatched")
            await until(lambda: list_hosts()[0].turns == 1)
            (entry,) = list_hosts()
            assert (entry.outcome, entry.unseen, entry.state) == ("done", True, "idle")
            # Looking at it again is what clears it.
            again, _, _ = await attach(host)
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
            terminal, _, _ = await attach(host)
            terminal.submit("hang long", "queue")
            await until(lambda: host.buffer)
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
            terminal, _, _ = await attach(host)
            terminal.runtime.stop(keep_worktree=True)
            await asyncio.wait_for(host.stopped.wait(), 5)
            assert host.keep_worktree
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
