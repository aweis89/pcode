"""Context is the last request's input, never accumulated billing totals."""

from unittest.mock import patch

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.usage import RequestUsage

from pcode.context_usage import compact_tokens, context_label, context_window


def response(tokens, **kwargs):
    return ModelResponse(parts=[], usage=RequestUsage(input_tokens=tokens, **kwargs))


def test_last_request_includes_cache_without_double_counting():
    history = [
        response(10_000),
        response(12_500, output_tokens=900, cache_read_tokens=8_000, cache_write_tokens=500),
        ModelRequest(parts=[UserPromptPart("next draft")]),
    ]
    with patch("pcode.context_usage.context_window", return_value=200_000):
        assert context_label("anthropic:example", history) == " · ctx: 12.5k/200k"
        # Checkout/resume uses the selected history; clearing doesn't retain usage.
        assert context_label("anthropic:example", history[:1]) == " · ctx: 10k/200k"
        assert context_label("anthropic:example", []) == " · ctx: 0/200k"
        assert context_label("anthropic:example", [response(0)]) == " · ctx: 0/200k"


def test_unknown_model_does_not_guess_capacity():
    assert context_window("unknown-provider:gpt-5") is None
    assert context_window("openai:nonexistent-model-for-test") is None
    assert context_window("unqualified-model") is None
    assert context_label("unknown-provider:gpt-5", [response(100)]) == " · ctx: 100/?"


def test_codex_never_borrows_openai_limits(monkeypatch):
    from pcode.model_metadata import ModelLimits, catalog

    catalog.public["openai:example"] = ModelLimits(context=1_050_000)
    assert context_window("openai:example") == 1_050_000
    assert context_window("openai-codex:example") is None


def test_display_and_compaction_share_override(monkeypatch):
    from pcode.compaction import effective_window

    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "272000")
    assert context_window("unknown-provider:example") == 272_000
    assert effective_window("unknown-provider:example") == 272_000
    assert context_label("unknown-provider:example", []) == " · ctx: 0/272k"


def test_invalid_override_does_not_crash_rendering(monkeypatch):
    from pcode.compaction import CompactionError, effective_window

    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "invalid")
    assert context_label("unknown-provider:example", []) == " · ctx: 0/?"
    with pytest.raises(CompactionError, match="positive token count"):
        effective_window("unknown-provider:example")


@pytest.mark.parametrize(
    "tokens, label",
    [
        (None, "?"),
        (0, "0"),
        (999, "999"),
        (1_000, "1k"),
        (12_345, "12.3k"),
        (200_000, "200k"),
        (1_000_000, "1m"),
        (1_050_000, "1.05m"),
    ],
)
def test_compact_tokens(tokens, label):
    assert compact_tokens(tokens) == label


def test_zero_usage_after_compaction_keeps_the_checkpoint_estimate():
    from pcode.compaction import MARKER, context_estimate

    checkpoint = response(50_000)
    checkpoint.metadata = {MARKER: {"tokens": 20_000}}
    history = [checkpoint, response(0)]
    with patch("pcode.context_usage.context_window", return_value=100_000):
        estimate = compact_tokens(context_estimate(history))
        assert context_label("test:example", history) == f" · ctx: ~{estimate}/100k"


def test_completed_request_updates_live_context_without_changing_replay_history():
    import asyncio
    from types import SimpleNamespace

    from pydantic_ai.models import ModelRequestParameters

    from pcode.compaction import ContextTracking

    history = [ModelRequest(parts=[UserPromptPart("do work")])]
    runtime = SimpleNamespace(history=[], session=object(), context_history=None)
    hook = ContextTracking(runtime)
    request = SimpleNamespace(messages=history, model_request_parameters=ModelRequestParameters())
    completed = response(12_500, cache_read_tokens=8_000)
    asyncio.run(hook.after_model_request(None, request_context=request, response=completed))
    with patch("pcode.context_usage.context_window", return_value=272_000):
        assert context_label("test:model", runtime.context_history) == " · ctx: 12.5k/272k"
    assert runtime.history == []
    assert len(history) == 1


@pytest.mark.parametrize("include_response", [False, True])
def test_context_without_reported_usage_shows_estimate(include_response):
    from pydantic_ai.messages import TextPart

    from pcode.compaction import context_estimate

    history = [ModelRequest(parts=[UserPromptPart("Explain this code in detail. " * 100)])]
    if include_response:
        # Some providers (and persisted histories) have content but no usage.
        history.append(ModelResponse(parts=[TextPart("Here is the explanation.")]))
    estimate = compact_tokens(context_estimate(history))
    with patch("pcode.context_usage.context_window", return_value=100_000):
        assert context_label("test:model", history) == f" · ctx: ~{estimate}/100k"
        history.append(response(2_000))
        assert context_label("test:model", history) == " · ctx: 2k/100k"


def test_pending_first_request_exposes_estimate_before_response():
    import asyncio
    from types import SimpleNamespace

    from pydantic_ai.models import ModelRequestParameters

    from pcode.compaction import ContextTracking, context_estimate

    history = [ModelRequest(parts=[UserPromptPart("Inspect the project and explain it. " * 100)])]
    runtime = SimpleNamespace(history=[], session=object(), context_history=None)
    request = SimpleNamespace(
        messages=history,
        model="unknown-provider:example",
        model_request_parameters=ModelRequestParameters(),
    )
    asyncio.run(ContextTracking(runtime).before_model_request(None, request))
    with patch("pcode.context_usage.context_window", return_value=100_000):
        estimate = compact_tokens(context_estimate(history))
        assert context_label("test:model", runtime.context_history) == f" · ctx: ~{estimate}/100k"
    assert runtime.history == []


def test_first_turn_context_is_live_without_autocompact():
    """The footer read `runtime.history`, which is empty until the first turn ends."""
    import asyncio

    from pydantic_ai import Agent
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    from pcode.compaction import SCHEMAS
    from pcode.live import AgentRuntime

    async def model(messages, info):
        if any(part.part_kind == "user-prompt" for part in messages[-1].parts):
            yield {0: DeltaToolCall(name="probe", json_args="{}")}
            return
        yield "done"

    agent = Agent(FunctionModel(stream_function=model))
    runtime = AgentRuntime(agent)
    runtime.auto_compact = False
    seen = []

    @agent.tool_plain
    def probe() -> str:
        # Mid-turn, while a tool runs: the request that called it is visible.
        seen.append(runtime.context_history)
        return "ok"

    async def exercise():
        async for _ in runtime.stream("start"):
            pass

    asyncio.run(exercise())
    assert seen and seen[0]
    assert isinstance(seen[0][-1], ModelResponse)
    assert SCHEMAS in (seen[0][-1].metadata or {})
    assert runtime.context_history is None
    assert SCHEMAS in (runtime.history[-1].metadata or {})
