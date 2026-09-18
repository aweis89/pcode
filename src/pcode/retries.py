"""Capture the exact request boundary, never replay completed tool work."""

from copy import deepcopy

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai_harness.step_persistence import is_provider_valid


class RequestCheckpoint(AbstractCapability):
    def __init__(self):
        self.messages = None
        self.step = 0

    async def wrap_model_request(self, ctx, *, request_context, handler):
        # All before-request hooks (including steering and compaction) have run.
        # Keep a copy: the graph mutates history while unwinding a broken stream.
        self.messages = (
            deepcopy(request_context.messages)
            if is_provider_valid(request_context.messages)
            else None
        )
        self.step = ctx.run_step
        response = await handler(request_context)
        self.messages = None
        return response
