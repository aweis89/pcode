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


def measured_context(message: ModelResponse) -> int | None:
    """The context fill a multi-step ACP turn (Antigravity) reported, if any.

    Such a turn reports cumulative billing across its internal requests in
    `usage.input_tokens`, which re-counts the context once per step and can be
    many times the window. `context_used` is the actual fill.
    """
    used = (message.provider_details or {}).get("context_used")
    return used if type(used) is int and used > 0 else None


def context_label(
    model: str | Model, history: Sequence[ModelMessage], *, compact_at: int | None = None
) -> str:
    """`· used/limit`, where the limit is the window or, when lower, `compact_at`."""
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
    if window is not None and compact_at:
        # Automatic compaction fires there, so that is the budget worth showing.
        window = min(window, compact_at)
    used = 0
    for message in reversed(history):
        if (message.metadata or {}).get(MARKER):
            return f" · ~{compact_tokens(context_estimate(history))}/{compact_tokens(window)}"
        if isinstance(message, ModelResponse):
            if context_used := measured_context(message):
                used = context_used
                break
            if message.usage.input_tokens:
                used = message.usage.input_tokens
                break
    if not used:
        # Streaming providers may not report usage until the response finishes.
        # Missing usage is not an empty context (also common in resumed history).
        estimate = context_estimate(history)
        if estimate:
            return f" · ~{compact_tokens(estimate)}/{compact_tokens(window)}"
    return f" · {compact_tokens(used)}/{compact_tokens(window)}"
