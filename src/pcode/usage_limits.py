"""Request-limit policy for delegated runs with shared usage accounting."""

from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, WrapRunHandler
from pydantic_ai.run import AgentRunResult


class UnlimitedRequests(AbstractCapability):
    """Remove the library's default request cap without isolating child usage."""

    async def wrap_run(self, ctx: RunContext, *, handler: WrapRunHandler) -> AgentRunResult[Any]:
        ctx.usage_limits.request_limit = None
        return await handler()
