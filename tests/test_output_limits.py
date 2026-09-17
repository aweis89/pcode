"""Output defaults must reach the adapter, not just the UI or parent agent."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.capabilities import CombinedCapability
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.usage import RunUsage

from pcode.agent import create_coder
from pcode.compaction import AutoCompaction
from pcode.model_metadata import ModelLimits
from pcode.output_limits import FALLBACK_OUTPUT_TOKENS, ModelOutputLimits


@pytest.fixture
def metadata(monkeypatch):
    refresh = AsyncMock()
    monkeypatch.setattr("pcode.output_limits.refresh_context", refresh)
    monkeypatch.setattr("pcode.model_metadata.refresh_context", refresh)
    limits = {}
    monkeypatch.setattr("pcode.output_limits.catalog.limits", lambda model: limits.get(id(model)))
    return limits, refresh


def model(name="claude-test", **kwargs):
    return AnthropicModel(name, provider=AnthropicProvider(api_key="synthetic", **kwargs))


def request(model, settings=None):
    return ModelRequestContext(
        model=model,
        messages=[ModelRequest(parts=[UserPromptPart("hello")])],
        model_settings=settings,
        model_request_parameters=ModelRequestParameters(),
    )


@pytest.mark.parametrize("maximum", [4096, 32_000, 128_000, None])
def test_model_limit_or_offline_fallback_without_mutating_settings(metadata, maximum):
    limits, refresh = metadata
    selected = model()
    limits[id(selected)] = ModelLimits(output=maximum)
    settings = {"anthropic_thinking": {"type": "adaptive"}, "temperature": 0.3}
    original = request(selected, settings)
    resolved = asyncio.run(ModelOutputLimits().before_model_request(None, original))
    assert resolved.model_settings == {**settings, "max_tokens": maximum or FALLBACK_OUTPUT_TOKENS}
    assert "max_tokens" not in settings
    assert original.model_settings is settings
    refresh.assert_awaited_once_with(selected)


@pytest.mark.parametrize("override", [0, 2048, 60_000])
def test_explicit_override_wins_without_metadata_lookup(metadata, override):
    original = request(model(), {"max_tokens": override})
    assert asyncio.run(ModelOutputLimits().before_model_request(None, original)) is original
    metadata[1].assert_not_awaited()


def test_other_providers_untouched_and_switches_recompute(metadata):
    limits, refresh = metadata
    capability = ModelOutputLimits()
    first, second = model("first"), model("second")
    limits[id(first)] = ModelLimits(output=128_000)
    limits[id(second)] = ModelLimits(output=8192)
    for selected, expected in [(first, 128_000), (second, 8192), (first, 128_000)]:
        resolved = asyncio.run(capability.before_model_request(None, request(selected)))
        assert resolved.model_settings["max_tokens"] == expected
    original = request(TestModel())
    assert asyncio.run(capability.before_model_request(None, original)) is original
    assert refresh.await_count == 3


def wire_response(body, blocks, *, truncated=False):
    message = {
        "id": "msg-test",
        "type": "message",
        "role": "assistant",
        "model": body["model"],
        "content": blocks,
        "stop_reason": "max_tokens"
        if truncated
        else ("tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": 125_350, "output_tokens": 4096 if truncated else 5000},
    }
    if not body.get("stream"):
        return httpx2.Response(200, json=message)
    events = [{"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}]
    for index, block in enumerate(blocks):
        events.append({"type": "content_block_start", "index": index, "content_block": block})
        events.append({"type": "content_block_stop", "index": index})
    events.extend(
        [
            {
                "type": "message_delta",
                "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
                "usage": message["usage"],
            },
            {"type": "message_stop"},
        ]
    )
    data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=data)


@pytest.mark.parametrize("streaming", [False, True])
def test_thinking_only_4096_failure_is_avoided_on_the_wire(metadata, streaming):
    seen = []

    def handle(req):
        body = json.loads(req.content)
        seen.append(body)
        truncated = body["max_tokens"] == 4096
        blocks = [{"type": "thinking", "thinking": "Reasoning", "signature": "synthetic"}]
        if not truncated:
            blocks.append({"type": "text", "text": "Completed"})
        return wire_response(body, blocks, truncated=truncated)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            selected = model(http_client=client)
            metadata[0][id(selected)] = ModelLimits(output=128_000)

            async def invoke(capabilities):
                agent = Agent(
                    selected,
                    capabilities=capabilities,
                    model_settings={"anthropic_thinking": {"type": "adaptive"}},
                )
                if streaming:
                    async with agent.run_stream("Think") as response:
                        return await response.get_output()
                return (await agent.run("Think")).output

            with pytest.raises(UnexpectedModelBehavior, match="token limit"):
                await invoke([])
            assert await invoke([ModelOutputLimits()]) == "Completed"
            assert seen[0]["max_tokens"] == 4096
            assert seen[-1]["max_tokens"] == 128_000
            assert all(b["thinking"] == {"type": "adaptive"} for b in seen)

    asyncio.run(run())


def test_coder_and_real_delegated_run_both_receive_limits(metadata, tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    seen = []

    def handle(req):
        body = json.loads(req.content)
        seen.append(body)
        parent = any(t["name"] == "delegate_task" for t in body.get("tools", []))
        already_delegated = any(
            isinstance(m["content"], list) and any(b["type"] == "tool_result" for b in m["content"])
            for m in body["messages"]
        )
        blocks = [{"type": "text", "text": "Done"}]
        if parent and not already_delegated:
            blocks = [
                {
                    "type": "tool_use",
                    "id": "delegate-1",
                    "name": "delegate_task",
                    "input": {"agent_name": "explorer", "task": "Inspect the repository"},
                }
            ]
        return wire_response(body, blocks)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            selected = model(http_client=client)
            metadata[0][id(selected)] = ModelLimits(output=32_000)
            result = await Agent(selected, capabilities=[create_coder(tmp_path)]).run("Explore")
            assert result.output == "Done"
            assert len(seen) == 3  # parent tool call, child answer, parent answer
            assert all(b["max_tokens"] == 32_000 for b in seen)

    asyncio.run(run())


@pytest.mark.parametrize(
    "window,used,compacts",
    [(200_000, 90_000, False), (200_000, 110_000, True), (16_000, 100, False)],
)
def test_compaction_sees_resolved_limit_and_small_windows_remain_usable(
    metadata, monkeypatch, window, used, compacts
):
    selected = model()
    metadata[0][id(selected)] = ModelLimits(output=128_000)
    runtime = SimpleNamespace(
        session=None, compaction_notice=lambda _: None, _compaction_usage=RunUsage()
    )
    monkeypatch.setattr("pcode.compaction.effective_window", lambda _: window)
    monkeypatch.setattr("pcode.compaction.context_estimate", lambda *args: used)
    summarize = AsyncMock(side_effect=RuntimeError("reached summarizer"))
    monkeypatch.setattr("pcode.compaction.summarize", summarize)
    # Reverse order deliberately: compaction must still run after limit resolution.
    combined = CombinedCapability([AutoCompaction(runtime, "test"), ModelOutputLimits()])
    assert isinstance(combined.capabilities[0], ModelOutputLimits)

    async def run():
        context = request(selected)
        for capability in combined.capabilities:
            context = await capability.before_model_request(
                SimpleNamespace(usage=RunUsage()), context
            )

    if compacts:
        with pytest.raises(RuntimeError, match="reached summarizer"):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert summarize.await_count == int(compacts)


def test_native_metadata_can_supply_output_without_input():
    from pcode.model_metadata import parse_anthropic

    limits = parse_anthropic({"max_input_tokens": None, "max_tokens": 32_000}, 123)
    assert limits == ModelLimits(output=32_000, source="anthropic", fetched_at=123)
    assert parse_anthropic({"max_input_tokens": None, "max_tokens": 0}, 123) is None
