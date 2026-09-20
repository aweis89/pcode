"""Slash enable authenticates independently of turns while the prompt stays usable."""

import asyncio
import json
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.mcp import config_path
from pcode.runtime import Message
from pcode.ui import create_prompt


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "burst",
        "failure",
        "cancel",
        "quit",
        "early-cancel",
        "early-cancel-queued",
        "slow-cancel",
    ],
)
def test_enable_login_is_immediate_cancellable_and_gates_prompts(outcome):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"mcpServers": {"remote": {"url": "https://example.invalid/mcp", "auth": "oauth"}}}
        )
    )

    async def run():
        started = asyncio.Event()
        finish = asyncio.Event()
        cleaned = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_finish = asyncio.Event()
        calls = []
        output = StringIO()

        class State:
            def __init__(self):
                self.enabled = {}

            async def enable(self, name):
                assert name == "remote"
                started.set()
                try:
                    await finish.wait()
                    if outcome == "failure":
                        raise RuntimeError("OAuth rejected")
                finally:
                    if outcome == "slow-cancel":
                        cleanup_started.set()
                        await cleanup_finish.wait()
                    cleaned.set()
                self.enabled[name] = object()

        class Runtime:
            session = None
            recovery_blocked = ""

            def __init__(self):
                self.mcp = State()

            async def stream(self, text):
                assert "remote" in self.mcp.enabled
                calls.append(text)
                yield Message("response")

        runtime = Runtime()
        app = PreviewApp(
            model="test:local",
            runtime=runtime,
            console=Console(file=output, color_system=None, width=140),
        )
        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            async def wait_for(predicate):
                async with asyncio.timeout(5):
                    while not predicate():
                        await asyncio.sleep(0.01)

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait_for(lambda: session is not None and session.app.is_running)
                    if outcome.startswith("early-cancel"):
                        pipe.send_text(
                            "/mcp enable remote\r"
                            + ("queued question\r" if outcome == "early-cancel-queued" else "")
                            + "\x03/mcp list\r"
                        )
                        await wait_for(lambda: "remote: off" in output.getvalue())
                        assert not started.is_set()
                        assert runtime.mcp.enabled == {}
                        assert calls == []
                        assert not app.activity.busy
                        pipe.send_text("/quit\r")
                        await asyncio.wait_for(task, 5)
                        return
                    pipe.send_text(
                        "/mcp enable remote\r" + ("queued question\r" if outcome == "burst" else "")
                    )
                    await asyncio.wait_for(started.wait(), 5)
                    assert calls == []
                    assert runtime.mcp.enabled == {}
                    assert app.activity.busy
                    assert "Enabling MCP" in app.activity.status
                    # Slash commands remain responsive while a browser login waits.
                    pipe.send_text("/mcp list\r")
                    await wait_for(lambda: "remote: off" in output.getvalue())
                    if outcome == "slow-cancel":
                        pipe.send_text("\x03")
                        await asyncio.wait_for(cleanup_started.wait(), 5)
                        pipe.send_text("\x03/quit\r")
                        await wait_for(lambda: not app.running)
                        assert not task.done()
                        assert not cleaned.is_set()
                        cleanup_finish.set()
                        await asyncio.wait_for(task, 5)
                        assert cleaned.is_set()
                        assert runtime.mcp.enabled == {}
                        assert calls == []
                        return
                    if outcome == "quit":
                        pipe.send_text("/quit\r")
                        await asyncio.wait_for(task, 5)
                        assert cleaned.is_set()
                        assert calls == []
                        assert runtime.mcp.enabled == {}
                        return
                    pipe.send_text(
                        ("" if outcome == "burst" else "queued question\r")
                        + "draft text\x1b[D\x1b[D\x1b[D\x1b[D"
                    )
                    await wait_for(lambda: session.default_buffer.text == "draft text")
                    assert app.activity.queued == 1
                    assert calls == []
                    if outcome == "cancel":
                        # The draft absorbs the first Ctrl+C; the login keeps waiting.
                        pipe.send_text("\x03")
                        await wait_for(lambda: not session.default_buffer.text)
                        assert app.activity.busy
                        pipe.send_text("\x03")
                    else:
                        finish.set()
                    await wait_for(lambda: not app.activity.busy)
                    assert cleaned.is_set()
                    assert app.activity.queued == 0
                    if outcome == "cancel":
                        pipe.send_text("draft text\x1b[D\x1b[D\x1b[D\x1b[D")
                        await wait_for(lambda: session.default_buffer.text == "draft text")
                    assert session.default_buffer.text == "draft text"
                    assert session.default_buffer.cursor_position == 6
                    if outcome in {"success", "burst"}:
                        assert calls == ["queued question"]
                        assert "remote" in runtime.mcp.enabled
                        assert "enabled for this conversation" in output.getvalue()
                    else:
                        assert calls == []
                        assert runtime.mcp.enabled == {}
                        await wait_for(lambda: "remains off" in " ".join(output.getvalue().split()))
                    pipe.send_text("\x03\x04")
                    await asyncio.wait_for(task, 5)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
        assert "No model request is made" in " ".join(output.getvalue().split())

    asyncio.run(run())
