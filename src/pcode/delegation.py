"""Forward bounded child activity without exposing child prose to the transcript.

Harness 0.31 supplies delegation lifecycle events and a child stream handler, but
not the parent's call identity in that handler. A context-local execution wrapper
pairs concurrent children with their own parent tool call. No global event queue
or mutable per-agent handler is needed, and cancellation resets the binding.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic

from pydantic_ai import CapabilityEvent, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
)

from pcode.inspection import capture
from pcode.runtime import ToolStarted, ToolSummary
from pcode.tool_display import result_detail, target

_parent: ContextVar[RunContext | None] = ContextVar("delegation_parent", default=None)


@dataclass(kw_only=True)
class ChildActivity(CapabilityEvent, namespace="pcode_delegation", name="activity"):
    activity: str
    child: ToolStarted | ToolSummary | None = None


class DelegationReporting(AbstractCapability):
    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        if call.tool_name != "delegate_task":
            return await handler(args)
        token = _parent.set(ctx)
        try:
            return await handler(args)
        finally:
            _parent.reset(token)


async def stream_child_activity(_ctx, events):
    """Consume child streams, emitting only tool boundaries and phase changes."""
    parent = _parent.get()
    tools = {}
    phase = ""
    async for event in events:
        if parent is None:
            continue
        child = None
        activity = phase
        parent_id = parent.tool_call_id or ""
        if isinstance(event, FunctionToolCallEvent):
            part = event.part
            try:
                args = part.args_as_dict()
            except (TypeError, ValueError):
                args = {}
            tools[part.tool_call_id] = (part.tool_name, args, monotonic())
            child = ToolStarted(
                part.tool_name,
                target(part.tool_name, args),
                f"{parent_id}:{part.tool_call_id}",
                arguments=capture(args),
                run_id=parent.run_id or "",
                started_at=datetime.now(timezone.utc).isoformat(),
                parent_call_id=parent_id,
            )
            activity = "Working"
        elif isinstance(event, FunctionToolResultEvent):
            name, args, started = tools.pop(
                event.tool_call_id, (event.part.tool_name or "tool", {}, monotonic())
            )
            outcome = "retry" if isinstance(event.part, RetryPromptPart) else event.part.outcome
            detail, failed = result_detail(name, args, event.part.content, outcome)
            child = ToolSummary(
                name,
                detail,
                failed=failed,
                call_id=f"{parent_id}:{event.tool_call_id}",
                elapsed_seconds=max(0, monotonic() - started),
                result=capture(event.part.content),
                run_id=parent.run_id or "",
                outcome=outcome,
                parent_call_id=parent_id,
            )
            activity = "Working" if tools else "Waiting for model"
        elif isinstance(event, PartStartEvent):
            if isinstance(event.part, ThinkingPart):
                activity = "Thinking"
            elif isinstance(event.part, TextPart):
                activity = "Responding"
        if child is not None or activity != phase:
            await parent.emit(ChildActivity(activity=activity, child=child))
            phase = activity
