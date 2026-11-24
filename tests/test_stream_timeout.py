"""A streamed request that produces nothing is abandoned and retried by the turn."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelRequestTimeout
from pydantic_ai.models.function import FunctionModel

from pcode import stream_timeout
from pcode.diagnostics import transient
from pcode.live import AgentRuntime, error_message
from pcode.stream_timeout import FirstEventTimeout


def test_silent_stream_is_retried_and_the_retry_answers(monkeypatch):
    monkeypatch.setattr(stream_timeout, "first_event_timeout", lambda: 0.2)
    calls = []
    notices = []

    async def respond(messages, info):
        calls.append(len(calls))
        if len(calls) == 1:
            await asyncio.sleep(30)  # accepted, then never streams
        yield "recovered"

    async def run():
        runtime = AgentRuntime(
            Agent(FunctionModel(stream_function=respond), capabilities=[FirstEventTimeout()])
        )
        runtime.retry_attempts = 1
        runtime.retry_notice = notices.append
        try:
            async with asyncio.timeout(10):
                async for _ in runtime.stream("hello"):
                    pass
        finally:
            runtime.close()

    asyncio.run(run())
    assert len(calls) == 2
    assert len(notices) == 1
    assert "without producing any output" in notices[0]
    assert "1/1" in notices[0]


def test_only_the_first_event_deadline_is_transient():
    assert transient(ModelRequestTimeout("m", 300, setting="first_event_timeout"))
    assert not transient(ModelRequestTimeout("m", 300, setting="request_timeout"))
    message = error_message(ModelRequestTimeout("gemini", 300, setting="first_event_timeout"))
    assert message == (
        "Request to model 'gemini' timed out after 300 seconds without producing any output."
    )


@dataclass
class _Context:
    model: SimpleNamespace
    model_settings: dict | None


def _context(system, settings=None):
    return _Context(SimpleNamespace(system=system), settings)


@pytest.mark.parametrize(
    ("system", "settings", "configured", "expected"),
    [
        ("google", None, 300.0, 300.0),
        ("google", {"first_event_timeout": 9}, 300.0, 9),
        ("google", None, 0.0, None),
        ("claude", None, 300.0, None),
        ("antigravity", None, 300.0, None),
    ],
)
def test_timeout_applies_unless_set_disabled_or_an_agent_process(
    monkeypatch, system, settings, configured, expected
):
    monkeypatch.setattr(stream_timeout, "first_event_timeout", lambda: configured)

    async def apply():
        context = _context(system, settings)
        result = await FirstEventTimeout().before_model_request(None, context)
        return (result.model_settings or {}).get("first_event_timeout")

    assert asyncio.run(apply()) == expected
