"""Inject pending steering messages between model requests, never mid-tool."""

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import UserPromptPart


class Steering(AbstractCapability):
    def __init__(self, take_messages):
        self.take_messages = take_messages

    async def before_model_request(self, ctx, request_context):
        # Append to the framework's request so tool results remain ahead of the
        # new user input and the input is included in persisted message history.
        for text in self.take_messages():
            request_context.messages[-1].parts.append(UserPromptPart(text))
        return request_context
