"""Translate Pydantic streams to UI-independent application events."""

from collections.abc import AsyncIterator
from uuid import uuid4

from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from pydantic_ai.messages import ModelMessage, RetryPromptPart
from pydantic_ai.usage import UsageLimits

from pcode.runtime import Event, Message, RunStatus, TextDelta, ToolSummary


class AgentRuntime:
    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.reset()

    def reset(self) -> None:
        self.history: list[ModelMessage] = []
        self.conversation_id = str(uuid4())
        self.turns = 0
        self.input_tokens = 0
        self.output_tokens = 0

    async def stream(self, prompt: str) -> AsyncIterator[Event]:
        emitted_text = False
        tools: dict[str, str] = {}
        # Unlike run_stream(), this completes the tool loop even when the model
        # sends explanatory text alongside its tool calls.
        async with self.agent.run_stream_events(
            prompt,
            message_history=self.history,
            conversation_id=self.conversation_id,
            usage_limits=UsageLimits(request_limit=30),
        ) as events:
            async for event in events:
                if isinstance(event, PartStartEvent):
                    if isinstance(event.part, TextPart):
                        yield TextDelta(event.part.content)
                    elif isinstance(event.part, ThinkingPart):
                        yield RunStatus("Thinking…")
                elif isinstance(event, PartDeltaEvent):
                    if isinstance(event.delta, TextPartDelta):
                        yield TextDelta(event.delta.content_delta)
                    elif isinstance(event.delta, ThinkingPartDelta):
                        yield RunStatus("Thinking…")
                elif isinstance(event, PartEndEvent) and isinstance(event.part, TextPart):
                    if event.part.content:
                        emitted_text = True
                        yield Message(event.part.content)
                elif isinstance(event, FunctionToolCallEvent):
                    tools[event.part.tool_call_id] = event.part.tool_name
                    yield RunStatus(f"Running {event.part.tool_name}…")
                elif isinstance(event, FunctionToolResultEvent):
                    name = tools.pop(event.tool_call_id, None) or event.part.tool_name or "tool"
                    outcome = (
                        "needs retry"
                        if isinstance(event.part, RetryPromptPart)
                        else event.part.outcome
                    )
                    # Tool contents remain in model history, not dumped into the
                    # terminal. Output may be large or contain sensitive material.
                    yield ToolSummary(
                        name,
                        "completed" if outcome == "success" else outcome,
                        failed=outcome != "success",
                    )
                    yield RunStatus("Waiting for model…")
                elif isinstance(event, AgentRunResultEvent):
                    result = event.result
                    if result.output and not emitted_text:
                        yield Message(str(result.output))
                    # Only commit history from complete runs. A failed/cancelled
                    # turn cannot leave unmatched tool calls in the next request.
                    self.history = result.all_messages()
                    self.turns += 1
                    self.input_tokens += result.usage.input_tokens
                    self.output_tokens += result.usage.output_tokens


def error_message(error: Exception) -> str:
    """Don't print raw provider bodies/validation inputs; they can contain secrets."""
    if isinstance(error, BaseExceptionGroup) and error.exceptions:
        return error_message(error.exceptions[0])
    name = type(error).__name__
    if name == "UserError" and "Codex CLI credentials" in str(error):
        return "Provider login missing or invalid. Run `codex login`, then restart pcode."
    if name == "CredentialsRefreshError":
        # Recognize only fixed public error codes, never echo token-endpoint bodies.
        for code in (
            "refresh_token_invalidated",
            "refresh_token_reused",
            "refresh_token_expired",
            "invalid_grant",
        ):
            if code in str(error):
                return (
                    f"Provider login is no longer valid ({code}). "
                    "Run `codex login`, then restart pcode."
                )
        return "Provider token refresh failed. Run `codex login`, then restart pcode."
    if "Credential" in name or "Authentication" in name:
        return f"Authentication failed ({name}). Refresh your provider login and restart."
    status = getattr(error, "status_code", None)
    if status is not None:
        return f"Provider request failed (HTTP {status}). Check model access and authentication."
    if isinstance(error, ImportError):
        return "Provider dependency missing. Install its pydantic-ai-slim extra and try again."
    return f"Run failed ({name}). Check the model string, provider credentials, and connectivity."
