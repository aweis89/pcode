"""Approximate request context, not cumulative session billing usage."""

from collections.abc import Sequence

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model

from pcode.model_metadata import ContextWindowError, context_window


def compact_tokens(tokens: int | None) -> str:
    if tokens is None:
        return "?"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.2f}".rstrip("0").rstrip(".") + "m"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(tokens)


def context_label(model: str | Model, history: Sequence[ModelMessage]) -> str:
    # Input usage already includes cache reads/writes in Pydantic AI. Do not add
    # them again, sum previous requests, or count output as input context.
    # Deriving from history also follows resume, /new, and conversation checkout.
    from pcode.compaction import MARKER, context_estimate

    try:
        window = context_window(model)
    except ContextWindowError:
        # Invalid configuration must not crash terminal rendering. Compaction
        # reports the actionable validation error when invoked.
        window = None
    used = 0
    for message in reversed(history):
        if (message.metadata or {}).get(MARKER):
            return f" · ~{compact_tokens(context_estimate(history))}/{compact_tokens(window)}"
        if isinstance(message, ModelResponse) and message.usage.input_tokens:
            used = message.usage.input_tokens
            break
    if not used:
        # Streaming providers may not report usage until the response finishes.
        # Missing usage is not an empty context (also common in resumed history).
        estimate = context_estimate(history)
        if estimate:
            return f" · ~{compact_tokens(estimate)}/{compact_tokens(window)}"
    return f" · {compact_tokens(used)}/{compact_tokens(window)}"
