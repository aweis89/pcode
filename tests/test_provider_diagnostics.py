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


def _mcp_error(module, name="McpError"):
    return type(name, (Exception,), {"__module__": module})("private-body")


def _raised_from(cause):
    try:
        raise RuntimeError("wrapper") from cause
    except RuntimeError as error:
        return error


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        # The SDK rejected Google's authorization server metadata mid-turn.
        (lambda: _mcp_error("mcp.client.auth.exceptions", "OAuthFlowError"), "sign-in failed"),
        (lambda: _mcp_error("fastmcp.client.auth.oauth", "ClientNotFoundError"), "sign-in failed"),
        (lambda: __import__("pcode.mcp_oauth").mcp_oauth.SignInRequired(), "sign-in failed"),
        (lambda: _raised_from(_mcp_error("mcp.client.auth.exceptions")), "sign-in failed"),
        (lambda: _mcp_error("mcp.shared.exceptions"), "server request failed"),
        (lambda: _mcp_error("fastmcp.exceptions", "ToolError"), "server request failed"),
        (lambda: _mcp_error("pydantic_ai.mcp", "MCPError"), "server request failed"),
    ],
)
def test_mcp_failures_do_not_blame_the_model(make, expected):
    message = error_message(make())
    assert expected in message
    assert "/mcp" in message
    assert "Check the model string" not in message
    assert "private-body" not in message


def test_mcp_match_is_by_package_not_prefix():
    assert "Check the model string" in error_message(_mcp_error("mcpx.errors"))


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
