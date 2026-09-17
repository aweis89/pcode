"""Context is the last request's input, never accumulated billing totals."""

from types import SimpleNamespace
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
        assert context_label("anthropic:example", history) == " · ctx: ~12.5k/200k"
        # Checkout/resume uses the selected history; clearing doesn't retain usage.
        assert context_label("anthropic:example", history[:1]) == " · ctx: ~10k/200k"
        assert context_label("anthropic:example", []) == " · ctx: ~?/200k"
        assert context_label("anthropic:example", [response(0)]) == " · ctx: ~?/200k"


def test_unknown_model_does_not_guess_capacity():
    assert context_window("unknown-provider:gpt-5") is None
    assert context_window("openai:nonexistent-model-for-test") is None
    assert context_window("unqualified-model") is None
    assert context_label("unknown-provider:gpt-5", [response(100)]) == " · ctx: ~100/?"


def test_codex_uses_openai_catalog_and_caches_lookup():
    context_window.cache_clear()
    with patch("pcode.context_usage.calc_price") as lookup:
        lookup.return_value = SimpleNamespace(model=SimpleNamespace(context_window=400_000))
        assert context_window("openai-codex:example") == 400_000
        assert context_window("openai-codex:example") == 400_000
        assert lookup.call_count == 1
        assert lookup.call_args.args[1] == "example"
        assert lookup.call_args.kwargs == {"provider_id": "openai"}
    context_window.cache_clear()


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
