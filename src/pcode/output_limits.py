"""Model-aware output defaults, separate from reasoning effort and visibility."""

from dataclasses import replace

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models.anthropic import AnthropicModel

from pcode.model_metadata import catalog, refresh_context

# Anthropic requires max_tokens. Pydantic AI 2.43/2.44 otherwise sends 4096,
# which can be exhausted entirely by thinking or a single file-writing tool call.
# Unknown/offline routes cannot safely borrow another endpoint's model limits.
FALLBACK_OUTPUT_TOKENS = 16_384


class ModelOutputLimits(AbstractCapability):
    """Resolve each request's default, including model switches and subagents.

    Use the serving model's advertised maximum, not a guessed family-name table.
    A maximum is a ceiling, not a requested response length or thinking budget.
    Other adapters retain their provider defaults; explicit settings always win.
    """

    async def before_model_request(self, ctx, request_context):
        if not isinstance(request_context.model, AnthropicModel):
            return request_context
        settings = request_context.model_settings or {}
        if settings.get("max_tokens") is not None:
            return request_context
        await refresh_context(request_context.model)
        limits = catalog.limits(request_context.model)
        maximum = limits.output if limits else None
        return replace(
            request_context,
            model_settings={**settings, "max_tokens": maximum or FALLBACK_OUTPUT_TOKENS},
        )
