"""Meridian routing and picker coverage; no real credentials or billed requests."""

import asyncio
import json
from io import StringIO

import httpx2
import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.agent import create_agent
from pcode.app import PreviewApp
from pcode.meridian import MeridianProvider, meridian_base_url
from pcode.model_ui import ModelPicker
from pcode.models import active_providers, model_catalog


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    for name in ("PCODE_LLM_PROXY", "PCODE_MERIDIAN_BASE_URL", "PCODE_MERIDIAN_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-upstream-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-upstream-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://upstream.invalid")


def test_picker_discovery(monkeypatch):
    monkeypatch.setattr("pcode.models.shutil.which", lambda _: "/bin/meridian")
    assert "meridian" in active_providers(None)
    monkeypatch.setattr("pcode.models.shutil.which", lambda _: None)
    monkeypatch.setenv("PCODE_MERIDIAN_BASE_URL", "http://localhost:4567")
    assert "meridian" in active_providers(None)
    monkeypatch.delenv("PCODE_MERIDIAN_BASE_URL")
    assert "meridian" in active_providers("meridian:custom")
    monkeypatch.setenv("PCODE_LLM_PROXY", "http://localhost:8080")
    assert "meridian" in active_providers("meridian:custom")


def test_picker_catalog_and_custom():
    names = model_catalog({"meridian"}, "meridian:custom")
    # Current custom IDs remain selectable without overriding newest-first sorting.
    assert "meridian:custom" in names
    assert "meridian:claude-opus-5" in names
    assert all(n.startswith("meridian:") for n in names)
    with create_pipe_input() as pipe:
        picker = ModelPicker(names, {"meridian"}, input=pipe, output=DummyOutput())
        picker.search.text = "meridian:custom-new-model"
        assert picker.matches == ["meridian:custom-new-model"]
        assert "Meridian" in str(picker.fragments())


@pytest.mark.parametrize(
    "url",
    ["ftp://localhost", "http://", "http://user:password@localhost", "http://localhost?key=value"],
)
def test_invalid_url_is_redacted(monkeypatch, url):
    monkeypatch.setenv("PCODE_MERIDIAN_BASE_URL", url)
    with pytest.raises(ValueError) as error:
        meridian_base_url()
    assert url not in str(error.value)


def test_switch_model_routes_stream_and_tools_to_meridian(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_MERIDIAN_BASE_URL", "http://127.0.0.1:4567")
    requests = []

    def handle(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["model"] == "claude-opus-5"
        assert body["tools"]
        assert body["stream"] is True
        assert str(request.url).split("?", 1)[0] == "http://127.0.0.1:4567/v1/messages"
        assert request.headers["x-meridian-agent"] == "passthrough"
        assert request.headers["x-api-key"] == "meridian-local"
        assert "authorization" not in request.headers
        events = [
            (
                "message_start",
                {
                    "message": {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-opus-5",
                        "content": [],
                        "usage": {"input_tokens": 1, "output_tokens": 0},
                    }
                },
            ),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "Hello"}}),
            ("content_block_stop", {"index": 0}),
            (
                "message_delta",
                {
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            ("message_stop", {}),
        ]
        data = "".join(
            f"event: {kind}\ndata: {json.dumps({'type': kind, **payload})}\n\n"
            for kind, payload in events
        )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=data)

    original = httpx2.AsyncClient

    class client(original):
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            super().__init__(**kwargs, transport=httpx2.MockTransport(handle))

    monkeypatch.setattr("pcode.meridian.httpx2.AsyncClient", client)
    app = PreviewApp(workspace=tmp_path, console=Console(file=StringIO()))

    async def run():
        await app.switch_model("meridian:claude-opus-5")
        assert app.model == "meridian:claude-opus-5"
        agent = app.runtime.agent
        assert isinstance(agent.model._provider, MeridianProvider)
        for _ in range(2):
            async with agent:
                async with agent.run_stream("hello") as result:
                    assert await result.get_output() == "Hello"
            assert agent.model._provider.client._client.is_closed
        assert len(requests) == 2
        app.runtime.close()

    asyncio.run(run())


def test_codex_proxy_does_not_affect_meridian(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_LLM_PROXY", "http://localhost:8080")
    agent = create_agent("meridian:claude-opus-5", tmp_path)
    assert isinstance(agent.model._provider, MeridianProvider)

    async def close():
        async with agent:
            pass

    asyncio.run(close())
