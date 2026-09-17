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
