import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import SlashCompleter
from pcode.live import AgentRuntime
from pcode.ui import create_prompt


def make_app(model="openai-codex:test", runtime=None):
    output = StringIO()
    runtime = runtime or SimpleNamespace(
        agent=SimpleNamespace(model=None, model_settings={"temperature": 0.5})
    )
    return PreviewApp(
        model=model, runtime=runtime, console=Console(file=output, color_system=None)
    ), output


def test_command_validation_completion_and_defaults():
    app, output = make_app()
    assert app.current_effort() == "default"
    for level in ("low", "medium", "high", "xhigh"):
        assert not app.handle(f"/effort {level}")
        assert app.current_effort() == level
        assert app.runtime.agent.model_settings["temperature"] == 0.5
    app.handle("/effort invalid")
    assert app.current_effort() == "xhigh"
    assert "Usage: /effort" in output.getvalue()
    app.handle("/effort")
    assert "Effort: xhigh" in output.getvalue()
    app.handle("/effort default")
    assert app.current_effort() == "default"
    assert app.runtime.agent.model_settings == {"temperature": 0.5}
    completions = list(
        SlashCompleter(app.registry).get_completions(Document("/effort "), CompleteEvent())
    )
    assert [item.text for item in completions] == ["low", "medium", "high", "xhigh", "default"]


@pytest.mark.parametrize("model", [None, "anthropic:test", "google:test"])
def test_unsupported_models_do_not_silently_change_settings(model):
    app, output = make_app(model)
    app.handle("/effort high")
    assert app.runtime.agent.model_settings == {"temperature": 0.5}
    assert "requires an OpenAI/Codex model" in output.getvalue()


def test_shortcuts_clamp_and_default_baseline():
    app, _ = make_app()
    app.adjust_effort(1)
    assert app.current_effort() == "high"
    app.adjust_effort(1)
    app.adjust_effort(1)
    assert app.current_effort() == "xhigh"
    app.effort("default")
    app.adjust_effort(-1)
    app.adjust_effort(-1)
    assert app.current_effort() == "low"


@pytest.mark.parametrize("busy", [False, True])
def test_real_keybindings_preserve_draft_and_cursor(busy):
    async def run():
        app, _ = make_app()
        app.activity.busy = busy
        app.effort("medium")
        with create_pipe_input() as pipe:
            session = create_prompt(
                app.registry,
                activity=app.activity,
                on_effort=app.adjust_effort,
                input=pipe,
                output=DummyOutput(),
            )
            task = asyncio.create_task(session.prompt_async())
            try:

                async def wait_for(predicate):
                    async with asyncio.timeout(5):
                        while not predicate():
                            await asyncio.sleep(0.01)

                await wait_for(lambda: session.app.is_running)
                pipe.send_text("draft\x1b[D\x0e")
                await wait_for(lambda: app.current_effort() == "high")
                assert session.default_buffer.text == "draft"
                assert session.default_buffer.cursor_position == 4
                pipe.send_text("\x10")
                await wait_for(lambda: app.current_effort() == "medium")
                assert session.default_buffer.text == "draft"
                assert session.default_buffer.cursor_position == 4
            finally:
                session.app.exit(result="")
                await task

    asyncio.run(run())


def test_effort_changes_apply_to_next_turn_not_next_tool_step():
    requests = []

    async def model(messages, info):
        requests.append(dict(info.model_settings))
        if len(requests) == 1:
            app.adjust_effort(1)
            yield {0: DeltaToolCall(name="ping", json_args="{}")}
        else:
            yield "Done."

    agent = Agent(FunctionModel(stream_function=model))

    @agent.tool_plain
    def ping() -> str:
        return "pong"

    runtime = AgentRuntime(agent)
    app, _ = make_app(runtime=runtime)
    app.effort("low")

    async def run():
        _ = [event async for event in runtime.stream("first")]
        _ = [event async for event in runtime.stream("second")]

    asyncio.run(run())
    assert [request["openai_reasoning_effort"] for request in requests] == ["low", "low", "medium"]
    runtime.reset()
    assert app.current_effort() == "medium"
