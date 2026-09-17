"""Approximate request context, not cumulative session billing usage."""

from collections.abc import Sequence
from functools import lru_cache

from genai_prices import Usage, calc_price
from pydantic_ai.messages import ModelMessage, ModelResponse


@lru_cache(maxsize=128)
def context_window(model: str) -> int | None:
    """Use bundled catalog metadata only; never fetch prices during rendering.

    Catalog limits may differ from account/API-specific limits. Unknown providers
    are not guessed from model names (a proxy may have different constraints).
    """
    provider, separator, name = model.partition(":")
    if not separator:
        return None
    if provider == "openai-codex":
        provider = "openai"
    try:
        return calc_price(
            Usage(input_tokens=0, output_tokens=0), name, provider_id=provider
        ).model.context_window
    except LookupError:
        return None


def compact_tokens(tokens: int | None) -> str:
    if tokens is None:
        return "?"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.2f}".rstrip("0").rstrip(".") + "m"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(tokens)


def context_label(model: str, history: Sequence[ModelMessage]) -> str:
    # Input usage already includes cache reads/writes in Pydantic AI. Do not add
    # them again, sum previous requests, or count output as input context.
    # Deriving from history also follows resume, /new, and conversation checkout.
    used = None
    for message in reversed(history):
        if isinstance(message, ModelResponse):
            # Zero commonly means a provider did not report usage.
            used = message.usage.input_tokens or None
            break
    return f" · ctx: ~{compact_tokens(used)}/{compact_tokens(context_window(model))}"
