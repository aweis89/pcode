"""Give up on a streamed request that produces nothing, so the turn retries it.

Provider clients only time out on their own read timeout, which for Google is
20 minutes: a Gemini request that was accepted but never streamed a byte sat
for all of it. Pydantic AI's `first_event_timeout` ends such a request with
`ModelRequestTimeout`, which `diagnostics.transient` lets the turn retry. It
only bounds the wait for the first output (thinking counts), so a long answer
or a pause in the middle of one runs on.

A per-request capability rather than agent settings, so delegated runs (which
get `model_settings=None`) and a mid-session `/model` switch are covered too.
"""

from dataclasses import replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import ModelRequestContext

# These drive an agent process that runs its own tools between events, so a
# silent stretch is not a dead request; each process has its own watchdogs.
AGENT_PROCESS_SYSTEMS = frozenset({"claude", "antigravity"})


def first_event_timeout() -> float:
    from pcode.preferences import SETTINGS, load_preferences

    setting = "first_event_timeout"
    return float(load_preferences().get(setting, SETTINGS[setting].default))


class FirstEventTimeout(AbstractCapability):
    """Apply the `first_event_timeout` preference to every request that sets none."""

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if request_context.model.system in AGENT_PROCESS_SYSTEMS:
            return request_context
        settings = request_context.model_settings or {}
        if "first_event_timeout" in settings:
            return request_context
        timeout = first_event_timeout()
        if timeout <= 0:
            return request_context
        return replace(request_context, model_settings={**settings, "first_event_timeout": timeout})
