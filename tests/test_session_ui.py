import asyncio
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from rich.console import Console

from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.session_ui import session_dialog, session_info_dialog
from pcode.sessions import SavedSession, SessionError, first_prompt


@pytest.mark.parametrize("keys,expected", [("\x1b[B\r", "second"), ("\x1b", None)])
def test_popup_keyboard(keys, expected):
    async def run():
        with create_pipe_input() as pipe:
            dialog = session_dialog(
                [("first", "First prompt"), ("second", "Second prompt")],
                input=pipe,
                output=DummyOutput(),
            )
            assert dialog.mouse_support()
            task = asyncio.create_task(dialog.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text(keys)
            assert await asyncio.wait_for(task, 2) == expected

    asyncio.run(run())


@pytest.mark.parametrize("keys", ["\x1b", "\r", "q"])
def test_info_popup_closes_on_every_exit_key(keys):
    async def run():
        with create_pipe_input() as pipe:
            dialog = session_info_dialog(
                [("Model", "test:local"), ("Turns", "2")],
                input=pipe,
                output=DummyOutput(),
            )
            task = asyncio.create_task(dialog.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text(keys)
            assert await asyncio.wait_for(task, 2) is None

    asyncio.run(run())


def test_session_overview_reports_storage_and_feeds_context(tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    stream = StringIO()
    app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=stream, width=200))
    app.model = "test:local"
    app.runtime = AgentRuntime(Agent("test"), saved)
    try:
        rows = dict(app.session_overview())
        assert rows["Model"] == "test:local"
        assert rows["Session"] == saved.info.id
        assert rows["Saved in"] == str(saved.directory)
        # /context prints exactly what the popup shows, so the two cannot drift.
        app.handle("/context")
        assert f"Saved in: {saved.directory}" in stream.getvalue()
    finally:
        app.runtime.close()


def test_session_command_requests_the_info_popup(tmp_path):
    app = PreviewApp(workspace=tmp_path, console=Console(file=StringIO()))
    app.handle("/session")
    assert app.session_info_requested
    assert not app.session_requested
    with pytest.raises(ValueError, match="Usage: /session"):
        app.registry.find("/session").handler("extra")


def test_first_prompt_is_not_latest_and_handles_empty_session(tmp_path):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        assert first_prompt(saved.info, saved.directory.parent) == "(No prompt yet)"
        saved.append("turn_started", prompt="First question")
        saved.append("turn_started", prompt="Followup")
        assert first_prompt(saved.info, saved.directory.parent) == "First question"
    finally:
        saved.close()


def test_resume_restores_before_replacing_runtime(tmp_path):
    async def run():
        root = tmp_path / "sessions"
        saved = SavedSession.create("test:local", tmp_path, root)
        identity = saved.info.id

        async def model(messages, info):
            yield "Saved answer"

        agent = Agent(FunctionModel(stream_function=model))
        previous = AgentRuntime(agent, saved)
        _ = [event async for event in previous.stream("First question")]
        history = previous.history
        previous.close()
        app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
        app.handle("/resume")
        assert app.session_requested
        with patch("pcode.agent.create_agent", return_value=agent):
            await app.resume_session(identity)
        try:
            assert app.runtime.session.info.id == identity
            assert app.runtime.conversation_id == identity
            assert app.model == "test:local"
            assert app.runtime.history == history
            assert app.runtime.turns == 1
            original = app.runtime
            await app.resume_session(identity)
            assert app.runtime is original
            other = SavedSession.create("test:local", tmp_path / "other", root)
            other_id = other.info.id
            other.close()
            with pytest.raises(SessionError, match="cross-repo"):
                await app.resume_session(other_id)
            assert app.runtime is original
            # The failed candidate's lock was released.
            reopened = SavedSession.open(other_id, root)
            reopened.close()
        finally:
            app.runtime.close()

    asyncio.run(run())


def test_failed_recovery_keeps_current_conversation(tmp_path):
    async def run():
        root = tmp_path / "sessions"
        saved = SavedSession.create("test:local", tmp_path, root)
        identity = saved.info.id
        saved.close()
        app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
        original = app.runtime
        with (
            patch("pcode.agent.create_agent", return_value=Agent("test")),
            patch("pcode.live.AgentRuntime.restore", side_effect=SessionError("broken")),
            pytest.raises(SessionError, match="broken"),
        ):
            await app.resume_session(identity)
        assert app.runtime is original
        reopened = SavedSession.open(identity, root)
        reopened.close()

    asyncio.run(run())
