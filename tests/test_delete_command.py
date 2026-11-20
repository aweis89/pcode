"""Tests for the /delete slash command."""

import asyncio
import subprocess
from io import StringIO
from unittest.mock import patch

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from rich.console import Console

from pcode.app import PreviewApp
from pcode.git_diff import load_review, mark_reviewed
from pcode.sessions import list_sessions
from pcode.ui import create_prompt


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _run_interactive(app: PreviewApp, body):
    """Drive `app.run_async()` with a pipe input; works both in-process and hosted."""

    async def runner():
        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                kwargs["input"] = pipe
                kwargs["output"] = DummyOutput()
                session = create_prompt(*args, **kwargs)
                return session

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    await _wait_for(lambda: session is not None and session.app.is_running)
                    await body(pipe)
                finally:
                    app.running = False
                    pipe.send_text("\x04")
                    await asyncio.wait_for(task, 5)

    asyncio.run(runner())


def test_delete_removes_saved_session_and_resets(tmp_path):
    root = tmp_path / "sessions"
    output = StringIO()

    async def model(messages, info):
        yield "answer"

    with patch(
        "pcode.agent.create_agent", return_value=Agent(FunctionModel(stream_function=model))
    ):
        app = PreviewApp(
            model="test:local",
            workspace=tmp_path,
            save=True,
            session_dir=root,
            console=Console(file=output, color_system=None),
        )

        async def scenario(pipe):
            pipe.send_text("hello\r")
            await _wait_for(lambda: len(list_sessions(root)) == 1 and "answer" in output.getvalue())
            await _wait_for(lambda: not app.activity.busy)
            first_id = list_sessions(root)[0].id
            assert (root / first_id).is_dir()

            pipe.send_text("/delete\r")
            await _wait_for(lambda: f"Deleted session {first_id[:8]}" in output.getvalue())
            assert not (root / first_id).exists()
            assert list_sessions(root) == []
            await _wait_for(lambda: app.runtime.session is None)
            assert app.activity.turn_outcome == ""

            # A follow-up prompt starts a fresh saved session with a new ID.
            pipe.send_text("after delete\r")
            await _wait_for(lambda: len(list_sessions(root)) == 1)
            await _wait_for(lambda: not app.activity.busy)
            second_id = list_sessions(root)[0].id
            assert second_id != first_id

        _run_interactive(app, scenario)


def test_delete_without_saved_session_errors(tmp_path):
    root = tmp_path / "sessions"
    output = StringIO()

    async def model(messages, info):
        yield "answer"

    with patch(
        "pcode.agent.create_agent", return_value=Agent(FunctionModel(stream_function=model))
    ):
        app = PreviewApp(
            model="test:local",
            workspace=tmp_path,
            save=True,
            session_dir=root,
            console=Console(file=output, color_system=None),
        )

        async def scenario(pipe):
            pipe.send_text("/delete\r")
            await _wait_for(lambda: "Nothing is saved yet" in output.getvalue())
            assert not root.exists()

        _run_interactive(app, scenario)


def test_delete_no_save_errors(tmp_path):
    root = tmp_path / "sessions"
    output = StringIO()

    async def model(messages, info):
        yield "answer"

    with patch(
        "pcode.agent.create_agent", return_value=Agent(FunctionModel(stream_function=model))
    ):
        app = PreviewApp(
            model="test:local",
            workspace=tmp_path,
            save=False,
            session_dir=root,
            console=Console(file=output, color_system=None),
        )

        async def scenario(pipe):
            pipe.send_text("hello\r")
            await _wait_for(lambda: "answer" in output.getvalue() and not app.activity.busy)
            pipe.send_text("/delete\r")
            await _wait_for(lambda: "Nothing is saved yet" in output.getvalue())

        _run_interactive(app, scenario)


def test_delete_rejects_arguments(tmp_path):
    root = tmp_path / "sessions"
    output = StringIO()

    async def model(messages, info):
        yield "answer"

    with patch(
        "pcode.agent.create_agent", return_value=Agent(FunctionModel(stream_function=model))
    ):
        app = PreviewApp(
            model="test:local",
            workspace=tmp_path,
            save=True,
            session_dir=root,
            console=Console(file=output, color_system=None),
        )

        async def scenario(pipe):
            pipe.send_text("/delete extra\r")
            await _wait_for(lambda: "Usage: /delete" in output.getvalue())

        _run_interactive(app, scenario)


def test_delete_refused_while_working(tmp_path):
    root = tmp_path / "sessions"
    output = StringIO()
    started = asyncio.Event()
    release = asyncio.Event()

    async def model(messages, info):
        started.set()
        await release.wait()
        yield "done"

    with patch(
        "pcode.agent.create_agent", return_value=Agent(FunctionModel(stream_function=model))
    ):
        app = PreviewApp(
            model="test:local",
            workspace=tmp_path,
            save=True,
            session_dir=root,
            console=Console(file=output, color_system=None),
        )

        async def scenario(pipe):
            pipe.send_text("working\r")
            await asyncio.wait_for(started.wait(), 5)
            pipe.send_text("/delete\r")
            await _wait_for(lambda: "/delete is unavailable while working" in output.getvalue())
            assert len(list_sessions(root)) == 1
            release.set()
            await _wait_for(lambda: "done" in output.getvalue() and not app.activity.busy)
            assert len(list_sessions(root)) == 1

        _run_interactive(app, scenario)


def test_delete_forgets_git_diff_ref(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "file.txt").write_text("hello\n")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    root = tmp_path / "sessions"
    output = StringIO()

    async def model(messages, info):
        yield "answer"

    with patch(
        "pcode.agent.create_agent", return_value=Agent(FunctionModel(stream_function=model))
    ):
        app = PreviewApp(
            model="test:local",
            workspace=repo,
            save=True,
            session_dir=root,
            console=Console(file=output, color_system=None),
        )

        async def scenario(pipe):
            pipe.send_text("hello\r")
            await _wait_for(lambda: len(list_sessions(root)) == 1 and "answer" in output.getvalue())
            await _wait_for(lambda: not app.activity.busy)
            session_id = list_sessions(root)[0].id
            (repo / "file.txt").write_text("changed\n")
            checkpoint = load_review(repo, [repo / "file.txt"]).checkpoint
            assert checkpoint is not None
            mark_reviewed(repo, session_id, checkpoint)
            refs = subprocess.run(
                ["git", "for-each-ref", "refs/pcode"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            assert session_id in refs.stdout

            pipe.send_text("/delete\r")
            await _wait_for(lambda: f"Deleted session {session_id[:8]}" in output.getvalue())
            assert not (root / session_id).exists()
            refs_after = subprocess.run(
                ["git", "for-each-ref", "refs/pcode"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            assert refs_after.stdout == ""

        _run_interactive(app, scenario)


def test_delete_preview_mode_errors(tmp_path):
    output = StringIO()
    app = PreviewApp(
        model=None,
        workspace=tmp_path,
        console=Console(file=output, color_system=None),
    )

    async def scenario(pipe):
        pipe.send_text("/delete\r")
        await _wait_for(lambda: "Nothing is saved yet" in output.getvalue())

    _run_interactive(app, scenario)


def test_delete_failure_warns_and_still_resets(tmp_path):
    from pcode.sessions import SessionError

    root = tmp_path / "sessions"
    output = StringIO()

    async def model(messages, info):
        yield "answer"

    with (
        patch("pcode.agent.create_agent", return_value=Agent(FunctionModel(stream_function=model))),
        patch("pcode.sessions.delete_session", side_effect=SessionError("permission denied")),
    ):
        app = PreviewApp(
            model="test:local",
            workspace=tmp_path,
            save=True,
            session_dir=root,
            console=Console(file=output, color_system=None),
        )

        async def scenario(pipe):
            pipe.send_text("hello\r")
            await _wait_for(lambda: len(list_sessions(root)) == 1 and "answer" in output.getvalue())
            await _wait_for(lambda: not app.activity.busy)
            first_id = list_sessions(root)[0].id

            pipe.send_text("/delete\r")
            await _wait_for(
                lambda: (
                    f"Could not delete session {first_id[:8]}: permission denied"
                    in output.getvalue()
                )
            )
            await _wait_for(lambda: app.runtime.session is None)

        _run_interactive(app, scenario)


def test_hosted_delete_clears_host_entry(tmp_path):
    from pcode.host import SessionHost
    from pcode.host_protocol import NOTHING_TO_RESUME, HostEntry
    from pcode.live import AgentRuntime
    from pcode.sessions import SavedSession

    root = tmp_path / "sessions"
    hosts = tmp_path / "hosts"

    async def model(messages, info):
        yield "answer"

    async def run():
        entry = HostEntry(id="h-del", pid=1, model="test:local", workspace=str(tmp_path))
        host = SessionHost(entry, directory=hosts)
        host.controller.model = "test:local"
        host.controller.workspace = tmp_path
        host.controller.runtime = AgentRuntime(
            Agent(FunctionModel(stream_function=model)),
            session_factory=lambda: SavedSession.create("test:local", tmp_path, root),
        )
        host.controller.ready.set()
        try:
            await host.controller.run_turn("hello")
            host.push_state()
            assert host.entry.session_id
            deleted_id = host.entry.session_id
            assert host.entry.last_prompt == "hello"

            await host.controller.dispatch("/delete", idle=True)
            assert not (root / deleted_id).exists()
            assert host.entry.session_id == ""
            assert host.entry.title == ""
            assert host.entry.last_prompt == ""
            assert host.entry.forked_from == ""
            assert host.restart() == {"skipped": NOTHING_TO_RESUME}
        finally:
            await host.close()

    asyncio.run(run())
