"""Count tokens as requests complete, not only when a run succeeds.

Usage was previously read from `AgentRunResultEvent`, which never arrives for a
turn that fails, is cancelled, or is retried: every request the provider had
already billed went unrecorded. `after_model_request` fires once per model
response, so a turn that dies mid-loop still accounts for what it spent.

Reads and writes are tracked separately because they are not interchangeable:
a cache write costs more than an uncached token and a read costs a fraction of
one, so a single "input" figure hides the difference this session was built to
watch.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RequestUsage


@dataclass
class TokenTotals:
    """Session totals. `input` includes cached reads and writes, as Pydantic AI defines it."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, usage: RequestUsage | Any) -> None:
        self.input += usage.input_tokens
        self.output += usage.output_tokens
        self.cache_read += usage.cache_read_tokens
        self.cache_write += usage.cache_write_tokens

    @property
    def uncached_input(self) -> int:
        return max(0, self.input - self.cache_read - self.cache_write)


@dataclass
class TokenAccounting(AbstractCapability):
    """Record each model response's usage through the run's own request hook.

    Installed on the parent run and on `SubAgents.shared_capabilities`, so a
    delegated request is counted exactly once, where it happens. Nothing else may
    add `AgentRunResult.usage` or `DelegationEndEvent.usage` on top: a child's
    tokens already aggregate into the parent's run usage.
    """

    record: Callable[[RequestUsage], None] = field(default=lambda usage: None)

    async def after_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        self.record(response.usage)
        # The hook is a filter, not a listener: returning None replaces the
        # model's response with nothing and the run fails on the next message.
        return response
