"""Thinking visibility requests Anthropic thinking without touching other routes."""

import asyncio
import json
from io import StringIO
from types import SimpleNamespace

import httpx2
import pytest
from anthropic import AsyncAnthropic
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from rich.console import Console

from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.preferences import apply_effort, apply_thinking, save_preferences


@pytest.mark.parametrize(
    "name, expected",
    [
        ("claude-sonnet-4-5", {"type": "enabled", "budget_tokens": 2048}),
        ("claude-sonnet-4-6", {"type": "adaptive"}),
        ("claude-opus-4-7", {"type": "adaptive"}),
    ],
)
@pytest.mark.parametrize("auth", ["api-key", "pi"])
def test_thinking_stream_request_and_transient_sink(name, expected, auth, tmp_path):
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        events = [
            (
                "message_start",
                {
                    "message": {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "model": name,
                        "content": [],
                        "usage": {"input_tokens": 1, "output_tokens": 0},
                    }
                },
            ),
        ]
        if "thinking" in body:
            events += [
                (
                    "content_block_start",
                    {
                        "index": 0,
                        "content_block": {
                            "type": "thinking",
                            "thinking": "",
                            "signature": "",
                        },
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "index": 0,
                        "delta": {
                            "type": "thinking_delta",
                            "thinking": "PRIVATE_THINKING",
                        },
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "index": 0,
                        "delta": {
                            "type": "signature_delta",
                            "signature": "test-signature",
                        },
                    },
                ),
                ("content_block_stop", {"index": 0}),
            ]
        events += [
            (
                "content_block_start",
                {
                    "index": 1,
                    "content_block": {
                        "type": "text",
                        "text": "",
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "index": 1,
                    "delta": {
                        "type": "text_delta",
                        "text": "Hello",
                    },
                },
            ),
            ("content_block_stop", {"index": 1}),
            (
                "message_delta",
                {
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 4},
                },
            ),
            ("message_stop", {}),
        ]
        data = "".join(
            f"event: {kind}\ndata: {json.dumps({'type': kind, **payload})}\n\n"
            for kind, payload in events
        )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=data)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            provider = AnthropicProvider(
                anthropic_client=AsyncAnthropic(
                    api_key="test-key",
                    auth_token="",
                    http_client=client,
                )
            )
            if auth == "pi":
                from pcode.pi_auth import PiAnthropicModel

                path = tmp_path / "pi.json"
                path.write_text(json.dumps({"anthropic": {"type": "api_key", "key": "test-key"}}))
                model = PiAnthropicModel(f"anthropic:{name}", path=path, http_client=client)
            else:
                model = AnthropicModel(name, provider=provider)
            agent = Agent(model)
            runtime = AgentRuntime(agent)
            app = PreviewApp(
                model=f"anthropic:{name}", runtime=runtime, console=Console(file=StringIO())
            )
            try:
                for shown in (False, True, False):
                    app.set_show_thinking(shown)
                    app.activity.thinking = ""
                    runtime.thinking_sink = app.activity.append_thinking
                    events = [event async for event in runtime.stream("hello")]
                    assert requests[-1]["stream"] is True
                    if shown:
                        assert requests[-1]["thinking"] == expected
                        if "budget_tokens" in expected:
                            assert expected["budget_tokens"] < requests[-1]["max_tokens"]
                        assert app.activity.thinking == "PRIVATE_THINKING"
                        assert app.activity.thinking_rows()
                    else:
                        assert "thinking" not in requests[-1]
                        assert app.activity.thinking == ""
                    assert not any("PRIVATE_THINKING" in repr(event) for event in events)
            finally:
                runtime.close()

    asyncio.run(run())


def test_startup_and_toggle_preserve_effort_and_replace_settings():
    save_preferences(show_thinking="on", effort="medium")
    agent = SimpleNamespace(model="anthropic:claude-opus-4-7", model_settings=None)
    app = PreviewApp(
        model=agent.model, runtime=SimpleNamespace(agent=agent), console=Console(file=StringIO())
    )
    assert agent.model_settings == {
        "anthropic_thinking": {"type": "adaptive"},
        "anthropic_effort": "medium",
    }
    captured = agent.model_settings
    app.show_thinking("off")
    assert agent.model_settings == {"anthropic_effort": "medium"}
    assert "anthropic_thinking" in captured
    app.set_show_thinking(True)  # Ctrl+T uses the same callback.
    apply_effort(agent, app.model, "high")
    assert agent.model_settings["anthropic_thinking"] == {"type": "adaptive"}
    assert "next turn" in app.transcript.console.file.getvalue()


def test_thinking_on_can_open_anthropic_without_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PCODE_ANTHROPIC_AUTH", raising=False)
    save_preferences(show_thinking="on")
    app = PreviewApp(
        model="anthropic:claude-opus-4-7", workspace=tmp_path, console=Console(file=StringIO())
    )
    asyncio.run(app._initialize_runtime())
    try:
        assert app.runtime.agent.model_settings["anthropic_thinking"] == {"type": "adaptive"}
    finally:
        app.runtime.close()


def test_switch_uses_current_visibility_not_saved_default(monkeypatch, tmp_path):
    app = PreviewApp(workspace=tmp_path, console=Console(file=StringIO()))
    app.set_show_thinking(True)
    save_preferences(show_thinking="off")
    monkeypatch.setattr("pcode.agent.create_agent", lambda *args: Agent("test"))

    async def run():
        await app.switch_model("anthropic:claude-sonnet-4-5")
        try:
            assert app.runtime.agent.model_settings["anthropic_thinking"] == {
                "type": "enabled",
                "budget_tokens": 2048,
            }
        finally:
            app.runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("model", ["meridian:claude-opus-4-7", "openai-codex:test", "google:test"])
def test_other_routes_are_untouched(model):
    settings = {"existing": "setting"}
    agent = SimpleNamespace(model_settings=settings)
    for shown in (True, False):
        apply_thinking(agent, model, shown)
        assert agent.model_settings is settings


def test_resume_applies_current_thinking_preference(monkeypatch, tmp_path):
    from pcode.sessions import SavedSession

    root = tmp_path / "sessions"
    saved = SavedSession.create("anthropic:claude-opus-4-7", tmp_path, root)
    identity = saved.info.id
    saved.close()
    app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
    app.set_show_thinking(True)
    from pydantic_ai.models.test import TestModel

    monkeypatch.setattr(
        "pcode.agent.create_agent",
        lambda *args: Agent(TestModel(profile={"anthropic_supports_adaptive_thinking": True})),
    )

    async def run():
        await app.resume_session(identity)
        try:
            assert app.runtime.agent.model_settings["anthropic_thinking"] == {"type": "adaptive"}
        finally:
            app.runtime.close()

    asyncio.run(run())
