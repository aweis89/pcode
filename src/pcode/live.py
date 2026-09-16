"""Translate Pydantic streams to UI-independent application events."""

import asyncio
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
from pydantic_ai_harness.step_persistence import StepPersistence

from pcode.diagnostics import error_details
from pcode.runtime import Event, Message, RunStatus, TextDelta, ToolSummary
from pcode.sessions import SavedSession, SessionError


class AgentRuntime:
    def __init__(self, agent: Agent, session: SavedSession | None = None) -> None:
        self.agent = agent
        self.session = session
        self._clear()

    def _clear(self) -> None:
        info = self.session.info if self.session else None
        self.history: list[ModelMessage] = []
        self.conversation_id = info.id if info else str(uuid4())
        self.turns = info.turns if info else 0
        self.input_tokens = info.input_tokens if info else 0
        self.output_tokens = info.output_tokens if info else 0
        self.recovery_blocked = ""

    def reset(self) -> None:
        if self.session:
            old = self.session
            from pathlib import Path

            self.session = SavedSession.create(
                old.info.model, Path(old.info.workspace), old.directory.parent
            )
            old.close()
        self._clear()

    async def restore(self) -> None:
        if self.session:
            self.history = await self.session.recover()

    def close(self) -> None:
        if self.session:
            self.session.close()

    async def stream(self, prompt: str) -> AsyncIterator[Event]:
        if self.recovery_blocked:
            raise SessionError(self.recovery_blocked)
        saved = self.session
        run_id = str(uuid4())
        if saved:
            saved.append("turn_started", prompt=prompt, run_id=run_id, sync=True)
            saved.info.status = "running"
            saved.save_info()
        try:
            async for event in self._stream(prompt, run_id):
                if saved:
                    saved.event(event)
                yield event
        except BaseException as error:
            if saved:
                cancelled = isinstance(
                    error, (asyncio.CancelledError, KeyboardInterrupt, GeneratorExit)
                )
                saved.append(
                    "turn_cancelled" if cancelled else "turn_failed",
                    run_id=run_id,
                    error=error_details(error),
                    sync=True,
                )
                saved.info.status = "cancelled" if cancelled else "failed"
                saved.save_info()
                try:
                    # Keep completed tool results even if the *following* request
                    # failed. Never silently re-run a side effect on retry.
                    self.history = await saved.recover()
                except SessionError as recovery_error:
                    self.recovery_blocked = str(recovery_error)
            raise
        else:
            if saved:
                saved.append("turn_completed", run_id=run_id, sync=True)
                saved.info.status = "complete"
                saved.info.turns = self.turns
                saved.info.input_tokens = self.input_tokens
                saved.info.output_tokens = self.output_tokens
                saved.save_info()

    async def _stream(self, prompt: str, run_id: str) -> AsyncIterator[Event]:
        emitted_text = False
        tools: dict[str, str] = {}
        # Unlike run_stream(), this completes the tool loop even when the model
        # sends explanatory text alongside its tool calls.
        async with self.agent.run_stream_events(
            prompt,
            message_history=self.history,
            conversation_id=self.conversation_id,
            run_id=run_id,
            capabilities=[StepPersistence(store=self.session.store)] if self.session else [],
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
                    # Full successful history. The outer persistence wrapper also
                    # recovers settled tool-boundary snapshots after failures.
                    self.history = result.all_messages()
                    self.turns += 1
                    self.input_tokens += result.usage.input_tokens
                    self.output_tokens += result.usage.output_tokens


def error_message(error: Exception) -> str:
    """Don't print raw provider bodies/validation inputs; they can contain secrets."""
    if isinstance(error, BaseExceptionGroup) and error.exceptions:
        return error_message(error.exceptions[0])
    name = type(error).__name__
    if isinstance(error, SessionError):
        return str(error)
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
        details = error_details(error)
        detail = details.get("provider_message", "")
        suffix = f" {detail}" if detail else " See the saved session diagnostics."
        return f"Provider request failed (HTTP {status}).{suffix}"
    if isinstance(error, ImportError):
        return "Provider dependency missing. Install its pydantic-ai-slim extra and try again."
    return f"Run failed ({name}). Check the model string, provider credentials, and connectivity."
