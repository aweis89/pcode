import asyncio
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import FunctionModel
from rich.console import Console

from pcode.app import PreviewApp
from pcode.jobs import JobRegistry
from pcode.preferences import load_preferences
from pcode.runtime import Message
from pcode.steering import Steering
from pcode.ui import create_prompt


def test_steering_is_in_model_input_and_history():
    from pydantic_ai.messages import ModelResponse, TextPart

    def model(messages, info):
        assert messages[-1].parts[-1].content == "change direction"
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(FunctionModel(model))
    result = agent.run_sync("original", capabilities=[Steering(lambda: ["change direction"])])
    assert any(
        isinstance(part, UserPromptPart) and part.content == "change direction"
        for message in result.all_messages()
        for part in message.parts
    )


@pytest.mark.parametrize("mode", ["steering", "queue", "interrupt"])
@pytest.mark.parametrize("editing_mode", ["emacs", "vi"])
def test_busy_send_modes(mode, editing_mode):
    from pcode.preferences import save_preferences

    save_preferences(editing_mode=editing_mode)

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []
        steered = []
        cancelled = []

        class Runtime:
            session = None
            recovery_blocked = ""
            jobs = JobRegistry(state=None)

            async def stream(self, text):
                calls.append(text)
                if text == "first":
                    started.set()
                    try:
                        await release.wait()
                        steered.extend(self.take_steering())
                        if mode == "steering":
                            assert app.activity.prompt == "second"
                            assert app.activity.prompt_state == "running"
                            assert not app.activity.queued_prompts
                            assert not app.activity.queued_modes
                    except asyncio.CancelledError:
                        cancelled.append(text)
                        raise
                yield Message("done")

        app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=StringIO()))
        app.send_mode = mode
        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            async def wait(predicate):
                async with asyncio.timeout(5):
                    while not predicate():
                        await asyncio.sleep(0.01)

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait(lambda: session is not None and session.app.is_running)
                    pipe.send_text("first\r")
                    await asyncio.wait_for(started.wait(), 5)
                    pipe.send_text("second\rdraft")
                    await wait(lambda: session.default_buffer.text == "draft")
                    if mode != "interrupt":
                        assert calls == ["first"]
                        assert app.activity.prompt == "first"
                        rows = app.activity.queue_rows(3)
                        label = "Steering (next model request)" if mode == "steering" else "Queued"
                        assert rows == [("class:plan", f"{label}: second")]
                        # Changing the selected mode must not relabel pending input.
                        app.send_mode = "interrupt"
                        assert app.activity.queue_rows(3) == rows
                        app.send_mode = mode
                        # Steering ends a foreground shell wait so the message
                        # reaches the model now; queue mode leaves it alone.
                        released = app.runtime.jobs.release_generation
                        assert released == (1 if mode == "steering" else 0)
                        release.set()
                    await wait(lambda: not app.activity.busy)
                    assert calls == (["first"] if mode == "steering" else ["first", "second"])
                    assert steered == (["second"] if mode == "steering" else [])
                    assert cancelled == (["first"] if mode == "interrupt" else [])
                    assert not app.activity.queued_prompts
                    assert session.default_buffer.text == "draft"
                    pipe.send_text("\x13")
                    expected = {"steering": "queue", "queue": "interrupt", "interrupt": "steering"}[
                        mode
                    ]
                    await wait(lambda: app.send_mode == expected)
                    assert load_preferences()["send_mode"] == expected
                    assert session.default_buffer.text == "draft"
                    pipe.send_text("\x03\x04")
                    await asyncio.wait_for(task, 5)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_steering_waits_for_tool_boundary():
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart

    pending = []
    requests = []

    def model(messages, info):
        requests.append(messages[-1])
        if len(requests) == 1:
            return ModelResponse(parts=[ToolCallPart("work", {}, "call")])
        parts = messages[-1].parts
        assert isinstance(parts[0], ToolReturnPart)
        assert isinstance(parts[-1], UserPromptPart)
        assert parts[-1].content == "new direction"
        return ModelResponse(parts=[TextPart("steered")])

    def take():
        result = pending[:]
        pending.clear()
        return result

    agent = Agent(FunctionModel(model))

    @agent.tool_plain
    def work() -> str:
        pending.append("new direction")
        return "tool completed"

    result = agent.run_sync("start", capabilities=[Steering(take)])
    assert result.output == "steered"
    assert len(requests) == 2
    assert not pending


def test_pending_rows_preserve_submission_modes():
    from pcode.ui import Activity

    activity = Activity(
        queued_prompts=["same", "same", "stop"],
        queued_modes=["queue", "steering", "interrupt"],
    )
    assert [text for _, text in activity.queue_rows(3)] == [
        "Queued: same",
        "Steering (next model request): same",
        "Interrupting: stop",
    ]
    assert activity.queue_rows(1) == [("class:plan", "… 3 more pending")]
