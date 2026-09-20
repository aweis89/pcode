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


@pytest.mark.parametrize("model", ["openai-codex:test", "anthropic:test", "meridian:test"])
def test_command_validation_completion_and_defaults(model):
    app, output = make_app(model)
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


@pytest.mark.parametrize("model", [None, "google:test"])
def test_unsupported_models_do_not_silently_change_settings(model):
    app, output = make_app(model)
    app.handle("/effort high")
    assert app.runtime.agent.model_settings == {"temperature": 0.5}
    assert "requires an OpenAI/Codex, Anthropic, or Meridian model" in output.getvalue()


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


@pytest.mark.parametrize(
    "provider, key",
    [
        ("openai-codex", "openai_reasoning_effort"),
        ("anthropic", "anthropic_effort"),
        ("meridian", "anthropic_effort"),
    ],
)
def test_effort_changes_apply_to_next_turn_not_next_tool_step(provider, key):
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
    app, _ = make_app(model=f"{provider}:test", runtime=runtime)
    app.effort("low")

    async def run():
        _ = [event async for event in runtime.stream("first")]
        _ = [event async for event in runtime.stream("second")]

    asyncio.run(run())
    assert [request[key] for request in requests] == ["low", "low", "medium"]
    runtime.reset()
    assert app.current_effort() == "medium"


@pytest.mark.parametrize("provider", ["anthropic", "meridian"])
@pytest.mark.parametrize("native_xhigh", [False, True])
def test_anthropic_effort_settings_and_restore(provider, native_xhigh):
    from pcode.preferences import apply_effort, effort_for

    app, _ = make_app(f"{provider}:test")
    agent = app.runtime.agent
    agent.model = SimpleNamespace(profile={"anthropic_supports_xhigh_effort": native_xhigh})
    original = agent.model_settings
    app.effort("xhigh")
    assert agent.model_settings == {
        "temperature": 0.5,
        "anthropic_effort": "xhigh" if native_xhigh else "max",
    }
    assert original == {"temperature": 0.5}
    assert app.current_effort() == "xhigh"
    agent.model_settings = {}
    apply_effort(agent, app.model, effort_for(app.model))
    assert app.current_effort() == "xhigh"
    app.adjust_effort(-1)
    assert agent.model_settings == {"anthropic_effort": "high"}
    app.effort("default")
    assert agent.model_settings == {}


@pytest.mark.parametrize("provider", ["anthropic", "meridian"])
@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "default"])
def test_anthropic_request_payload(provider, level):
    import json

    import httpx2
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": [{"type": "text", "text": "Done."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
            client = AsyncAnthropic(api_key="synthetic", http_client=http)
            model = AnthropicModel(
                "claude-opus-4-6", provider=AnthropicProvider(anthropic_client=client)
            )
            agent = Agent(model)
            app, _ = make_app(f"{provider}:claude-opus-4-6", AgentRuntime(agent))
            app.effort(level)
            await agent.run("Hello")

    asyncio.run(run())
    body = requests[0]
    if level == "default":
        assert "effort" not in body.get("output_config", {})
    else:
        assert body["output_config"]["effort"] == ("max" if level == "xhigh" else level)
    assert "openai_reasoning_effort" not in body
