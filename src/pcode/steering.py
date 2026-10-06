"""Deliver pending steering between model requests; never skip tools."""

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import UserPromptPart


class Steering(AbstractCapability):
    def __init__(self, take_messages, has_messages=lambda: False):
        self.take_messages = take_messages
        # A peek that consumes nothing, for waits that should hand back early.
        self.has_messages = has_messages

    async def before_model_request(self, ctx, request_context):
        # Append to the framework's request so tool results remain ahead of the
        # new user input and the input is included in persisted message history.
        for text in self.take_messages():
            request_context.messages[-1].parts.append(UserPromptPart(text))
        return request_context


def steering_pending(ctx) -> bool:
    """Inspect this run only, never a parent's or a concurrent side question's input."""
    return any(
        capability.has_messages()
        for capability in ctx.capabilities.values()
        if isinstance(capability, Steering)
    )
