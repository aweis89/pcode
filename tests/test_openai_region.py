"""A data-residency OpenAI project must work without the user finding its host."""

import asyncio
import json
import weakref

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from pcode import openai_region
from pcode.live import AgentRuntime
from pcode.model_metadata import catalog_key
from pcode.openai_region import regional_host, track

REJECTION = {
    "error": {
        "message": "Attempted to access resource with incorrect regional hostname. "
        "Please make your request to us.api.openai.com",
        "type": "invalid_request_error",
        "code": "incorrect_hostname",
        "param": None,
    }
}
CHUNK = {
    "id": "c",
    "object": "chat.completion.chunk",
    "created": 0,
    "model": "test-model",
    "choices": [
        {"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
}


@pytest.fixture(autouse=True)
def fresh_region(monkeypatch):
    """Each test starts in a process that has not learned a region."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(openai_region, "_regional_url", None)
    monkeypatch.setattr(openai_region, "_clients", weakref.WeakSet())


def rejection(message: str, code: str = "incorrect_hostname") -> ModelHTTPError:
    return ModelHTTPError(401, "m", body={"code": code, "message": message})


def test_only_a_named_openai_host_is_followed():
    assert regional_host(rejection(REJECTION["error"]["message"])) == "us.api.openai.com"
    assert regional_host(rejection("Use eu.api.openai.com.")) == "eu.api.openai.com"
    assert regional_host(rejection("Use https://US.API.OpenAI.com/v1")) == "us.api.openai.com"
    # Only the matched host is used to build the URL, so a suffix cannot redirect.
    assert regional_host(rejection("Use us.api.openai.com.evil.example")) == "us.api.openai.com"
    # The body is untrusted: a host elsewhere would receive the API key.
    assert regional_host(rejection("Use api.openai.com.evil.example")) is None
    assert regional_host(rejection("Use evil-api.openai.com")) is None
    assert regional_host(rejection("Use api.openai.com")) is None
    assert regional_host(rejection("Use us.api.openai.com", code="invalid_api_key")) is None
    assert regional_host(ModelHTTPError(401, "m", body="incorrect_hostname")) is None


def reply(request):
    if request.url.host != "us.api.openai.com":
        return httpx2.Response(401, json=REJECTION)
    if request.url.path.endswith("/responses"):
        response = {
            "id": "r",
            "object": "response",
            "created_at": 0,
            "model": "test-model",
            "status": "in_progress",
            "output": [],
        }
        item = {
            "type": "message",
            "id": "m",
            "role": "assistant",
            "status": "completed",
            "content": [],
        }
        events = [
            {"type": "response.created", "response": response},
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {
                "type": "response.output_text.delta",
                "item_id": "m",
                "output_index": 0,
                "content_index": 0,
                "delta": "ok",
            },
            {
                "type": "response.completed",
                "response": {**response, "status": "completed", "output": [item]},
            },
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    else:
        body = f"data: {json.dumps(CHUNK)}\n\ndata: [DONE]\n\n"
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def run_turns(handle, notices, *, kind=OpenAIChatModel, base_url=None, runtimes=1):
    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:

            def build():
                return kind(
                    "test-model",
                    provider=OpenAIProvider(
                        api_key="synthetic", http_client=client, base_url=base_url
                    ),
                )

            # Built before the 401, as the sub-agent menu is at startup.
            earlier = track(build())
            # A later runtime is a rebuilt agent, as /model or resume makes.
            for _ in range(runtimes):
                runtime = AgentRuntime(Agent(build()))
                runtime.retry_attempts = 0
                runtime.retry_notice = notices.append
                try:
                    async for _ in runtime.stream("hello"):
                        pass
                    async for _ in runtime.stream("again"):
                        pass
                finally:
                    runtime.close()
            # And one built after it, as compaction or a side model would be.
            return earlier, track(build())

    return asyncio.run(run())


@pytest.mark.parametrize("kind", [OpenAIChatModel, OpenAIResponsesModel])
def test_the_first_request_follows_the_regional_host_and_later_ones_start_there(kind):
    hosts, notices = [], []

    def handle(request):
        hosts.append(request.url.host)
        return reply(request)

    earlier, later = run_turns(handle, notices, kind=kind)

    assert hosts == ["api.openai.com", "us.api.openai.com", "us.api.openai.com"]
    assert len(notices) == 1 and "us.api.openai.com" in notices[0]
    for model in (earlier, later):
        assert str(model.client.base_url) == "https://us.api.openai.com/v1/"
        # The public catalog still describes it: same models, another host.
        assert catalog_key(model) == "openai:test-model"


def test_a_rebuilt_agent_starts_on_the_learned_host():
    hosts, notices = [], []

    def handle(request):
        hosts.append(request.url.host)
        return reply(request)

    run_turns(handle, notices, runtimes=2)

    assert hosts == ["api.openai.com"] + ["us.api.openai.com"] * 4
    assert len(notices) == 1


def test_a_users_own_base_url_is_never_overridden(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/v1")
    hosts, notices = [], []

    def handle(request):
        hosts.append(request.url.host)
        return httpx2.Response(401, json=REJECTION)

    with pytest.raises(ModelHTTPError):
        run_turns(handle, notices)

    assert hosts == ["gateway.example"]
    assert notices == []


def test_a_proxy_passing_the_rejection_through_is_left_alone():
    hosts, notices = [], []

    def handle(request):
        hosts.append(request.url.host)
        return httpx2.Response(401, json=REJECTION)

    with pytest.raises(ModelHTTPError):
        run_turns(handle, notices, base_url="https://proxy.example/v1")

    assert hosts == ["proxy.example"]
    assert notices == []
    assert openai_region._regional_url is None


def test_a_regional_host_that_also_rejects_fails_instead_of_looping():
    hosts, notices = [], []

    def handle(request):
        hosts.append(request.url.host)
        return httpx2.Response(401, json=REJECTION)

    with pytest.raises(ModelHTTPError):
        run_turns(handle, notices)

    assert hosts == ["api.openai.com", "us.api.openai.com"]
