"""Give up on a streamed request that produces nothing, so the turn retries it.

Provider clients only time out on their own read timeout, which for Google is
20 minutes: a Gemini request that was accepted but never streamed a byte sat
for all of it. Pydantic AI's `first_event_timeout` ends such a request with
`ModelRequestTimeout`, which `diagnostics.transient` lets the turn retry. It
only bounds the wait for the first output (thinking counts), so a long answer
or a pause in the middle of one runs on.

A per-request capability rather than agent settings, so delegated runs (which
get `model_settings=None`) and a mid-session `/model` switch are covered too.
Compaction's summary agent has neither, so `summary_settings` adds it itself.
"""

from dataclasses import replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import ModelRequestContext

# These drive an agent process that runs its own tools between events, so a
# silent stretch is not a dead request; each process has its own watchdogs.
from pcode.claude_sdk.workspace import CLI_SYSTEMS


def first_event_timeout() -> float:
    from pcode.preferences import SETTINGS, load_preferences

    setting = "first_event_timeout"
    return float(load_preferences().get(setting, SETTINGS[setting].default))


def timeout_settings(model) -> dict:
    """`{"first_event_timeout": seconds}` for `model`, or `{}` where none applies."""
    if getattr(model, "system", None) in CLI_SYSTEMS:
        return {}
    timeout = first_event_timeout()
    return {"first_event_timeout": timeout} if timeout > 0 else {}


class FirstEventTimeout(AbstractCapability):
    """Apply the `first_event_timeout` preference to every request that sets none."""

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        settings = request_context.model_settings or {}
        if "first_event_timeout" in settings:
            return request_context
        extra = timeout_settings(request_context.model)
        if not extra:
            return request_context
        return replace(request_context, model_settings={**settings, **extra})
