"""Async /compact owns the same terminal cancellation/queue gates as model runs."""

import asyncio
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from rich.console import Console

from pcode.app import PreviewApp
from pcode.compaction import CompactionResult
from pcode.live import AgentRuntime
from pcode.preferences import load_preferences
from pcode.runtime import Message
from pcode.ui import create_prompt


@pytest.mark.parametrize(
    "outcome", ["success", "burst", "failure", "cancel", "early-cancel", "quit"]
)
def test_compact_cancellation_busy_gates_and_prompt_queue(outcome):
    async def run():
        started = asyncio.Event()
        finish = asyncio.Event()
        cleaned = asyncio.Event()
        calls = []
        output = StringIO()

        class Runtime:
            session = None
            recovery_blocked = ""

            async def compact(self, focus):
                calls.append(("compact", focus))
                started.set()
                try:
                    await finish.wait()
                    if outcome == "failure":
                        raise ValueError("summary unavailable")
                    return CompactionResult([], 50000, 12000, True)
                finally:
                    cleaned.set()

            async def stream(self, text):
                assert cleaned.is_set()
                calls.append(("prompt", text))
                yield Message("response")

        app = PreviewApp(
            model="test:local",
            runtime=Runtime(),
            console=Console(file=output, color_system=None, width=140),
        )
        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            async def wait_for(predicate):
                for _ in range(500):
                    if predicate():
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError(f"Timed out: {output.getvalue()}")

            async def drive():
                await wait_for(lambda: session is not None and session.app.is_running)
                if outcome == "early-cancel":
                    pipe.send_text("/compact keep tests\r\x03")
                    await wait_for(lambda: "Pending compaction cancelled" in output.getvalue())
                    pipe.send_text("/quit\r")
                    return
                pipe.send_text(
                    "/compact keep {tests}\r"
                    + ("continue after summary\r" if outcome == "burst" else "")
                )
                await started.wait()
                pipe.send_text("/new\r/tree\r/compact again\r")
                await wait_for(lambda: "/tree is unavailable" in output.getvalue())
                assert calls == [("compact", "keep {tests}")]
                if outcome == "quit":
                    pipe.send_text("/quit\r")
                    await cleaned.wait()
                    return
                if outcome != "burst":
                    pipe.send_text("continue after summary\r")
                await wait_for(lambda: bool(app.activity.queued_prompts))
                assert len(calls) == 1
                if outcome == "cancel":
                    pipe.send_text("\x03")
                else:
                    finish.set()
                await cleaned.wait()
                await wait_for(lambda: not app.activity.busy)
                if outcome in {"success", "burst"}:
                    assert ("prompt", "continue after summary") in calls
                else:
                    assert len(calls) == 1
                    assert not app.activity.queued_prompts
                pipe.send_text("/quit\r")

            with patch("pcode.app.create_prompt", prompt):
                await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=10)
        text = output.getvalue()
        if outcome == "early-cancel":
            assert not calls
        elif outcome in {"success", "burst"}:
            assert "50k → ~12k" in text
        elif outcome == "failure":
            assert "Compaction failed" in text
        else:
            assert "Compaction cancelled" in text

    asyncio.run(run())


def test_manual_command_preview_rejection_and_autocompact_preference(monkeypatch):
    preview = PreviewApp(console=Console(file=StringIO()))
    with pytest.raises(ValueError, match="live model"):
        preview.compact("focus")
    runtime = AgentRuntime(Agent(TestModel()))
    app = PreviewApp(model="test:local", runtime=runtime, console=Console(file=StringIO()))
    assert not runtime.auto_compact
    monkeypatch.delenv("PCODE_CONTEXT_WINDOW", raising=False)
    with pytest.raises(ValueError, match="Unknown context window"):
        app.autocompact("on")
    assert load_preferences().get("autocompact") is None
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "128000")
    app.autocompact("on")
    assert runtime.auto_compact
    assert load_preferences()["autocompact"] == "on"
    assert AgentRuntime(Agent(TestModel())).auto_compact
    app.autocompact("off")
    assert not runtime.auto_compact
    assert load_preferences()["autocompact"] == "off"
    app.registry.dispatch("/compact retain exact {identifiers}")
    assert app.compact_requested == "retain exact {identifiers}"
    app.activity.busy = True
    with pytest.raises(ValueError, match="idle"):
        app.autocompact("on")
