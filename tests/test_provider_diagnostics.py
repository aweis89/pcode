import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from openai import APIError
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import FunctionModel

from pcode.diagnostics import provider_context
from pcode.live import AgentRuntime, error_message
from pcode.sessions import SavedSession


@pytest.mark.parametrize("provider", ["openai", "openai-codex", "anthropic"])
def test_provider_route_strips_url_credentials(provider):
    model = SimpleNamespace(
        model_name="example",
        provider=SimpleNamespace(
            name=provider,
            client=SimpleNamespace(
                base_url="https://user:password@example.com/v1?credential=unknown#secret"
            ),
        ),
    )
    assert provider_context(model) == {
        "model": "example",
        "provider": provider,
        "base_url": "https://example.com/v1",
    }
    assert provider_context("openai-codex:example") == {"model": "openai-codex:example"}
    assert provider_context(None) == {}


def test_stream_billing_error_and_wrapped_causes():
    error = APIError(
        "You have no credits remaining. private-body",
        request=httpx2.Request("POST", "https://example.com"),
        body={"message": "You have no credits remaining. private-body"},
    )
    wrapper = RuntimeError("wrapper")
    wrapper.__cause__ = error
    for item in (error, wrapper, ExceptionGroup("group", [error])):
        message = error_message(item)
        assert "quota or credits exhausted" in message
        assert "private-body" not in message
        assert "Check the model string" not in message


@pytest.mark.parametrize(
    "body, expected",
    [
        ({"error": {"type": "rate_limit_error", "message": "private-body"}}, "rate limit reached"),
        ({"error": {"code": "insufficient_quota"}}, "quota or credits exhausted"),
    ],
)
def test_http_quota(body, expected):
    message = error_message(ModelHTTPError(429, "example", body))
    assert expected in message
    assert "private-body" not in message


def test_unrelated_error_keeps_generic_guidance():
    assert "Check the model string" in error_message(RuntimeError("unrelated"))


def test_failed_turn_records_configured_route(tmp_path):
    async def fail(messages, info):
        raise RuntimeError("failure")
        yield "unreachable"

    model = FunctionModel(stream_function=fail)
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    runtime = AgentRuntime(Agent(model), saved)

    async def run():
        with pytest.raises(RuntimeError):
            _ = [event async for event in runtime.stream("hello")]

    try:
        asyncio.run(run())
        records = [
            json.loads(line)
            for line in (saved.directory / "transcript.jsonl").read_text().splitlines()
        ]
        failure = next(r for r in records if r["kind"] == "turn_failed")
        assert failure["provider_context"] == provider_context(model)
        report = (saved.directory / "errors.log").read_text()
        assert "Configured provider:" in report
        assert model.model_name in report
    finally:
        runtime.close()
