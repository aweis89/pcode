"""New conversations persist only when a model prompt is submitted."""

import asyncio
from io import StringIO
from unittest.mock import patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from rich.console import Console

from pcode.app import PreviewApp
from pcode.sessions import SavedSession, list_sessions


@pytest.mark.parametrize("save", [True, False])
def test_first_prompt_creates_session_once(tmp_path, save):
    root = tmp_path / "sessions"

    async def model(messages, info):
        yield "answer"

    output = StringIO()
    with patch(
        "pcode.agent.create_agent", return_value=Agent(FunctionModel(stream_function=model))
    ):
        app = PreviewApp(
            model="test:local",
            workspace=tmp_path,
            save=save,
            session_dir=root,
            console=Console(file=output, color_system=None),
        )
    try:
        for text in ("", "   ", "/help", "/context", "/session", "/new", "/new"):
            assert not app.handle(text)
        assert app.runtime.session is None
        assert not root.exists()
        assert ("after your first prompt" in output.getvalue()) is save

        async def submit():
            for text in ("first question", "follow up"):
                assert app.handle(text)
                _ = [event async for event in app.runtime.stream(text)]

        asyncio.run(submit())
        if save:
            saved = app.runtime.session
            assert app.runtime.conversation_id == saved.info.id
            assert len(list_sessions(root)) == 1
            assert saved.info.turns == 2
            assert [
                r["prompt"] for r in saved.recent_transcript() if r["kind"] == "turn_started"
            ] == [
                "first question",
                "follow up",
            ]
            identity = saved.info.id
            app.runtime.close()
            reopened = SavedSession.open(identity, root)
            reopened.close()
        else:
            assert app.runtime.session is None
            assert not root.exists()
    finally:
        app.runtime.close()


def test_quitting_without_prompt_does_not_create_session(tmp_path):
    root = tmp_path / "sessions"
    with patch("pcode.agent.create_agent", return_value=Agent("test")):
        app = PreviewApp(
            model="test:local",
            workspace=tmp_path,
            save=True,
            session_dir=root,
            console=Console(file=StringIO()),
        )
    app.handle("/quit")
    app.runtime.close()
    assert not root.exists()
