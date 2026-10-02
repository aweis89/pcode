"""Translate one agent run's Pydantic AI events into pcode's runtime events.

`agent.run_stream_events` yields typed upstream events: model parts streaming
in, tool calls and results, and the harness's shell, delegation and file
events. `EventTranslator.translate` turns each into the plain dataclasses in
`pcode.runtime` that the terminal renders. It holds the per-run bookkeeping
that pairs a tool's start with its end.
"""

import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import datetime, timezone
from time import monotonic
from typing import TYPE_CHECKING, Any

from pydantic_ai import (
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
from pydantic_ai.messages import NativeToolCallPart, NativeToolReturnPart, RetryPromptPart
from pydantic_ai_harness.shell import (
    CommandFinishedEvent,
    CommandOutputEvent,
    CommandStartedEvent,
)
from pydantic_ai_harness.subagents import DelegationEndEvent, DelegationStartEvent

from pcode.cache_warnings import CacheBustEvent
from pcode.delegation import ChildActivity, ChildOutput
from pcode.edit_preview import StreamingEditPreview
from pcode.filesystem import FileChangeEvent
from pcode.inspection import capture
from pcode.plan_preview import StreamingPlanPreview
from pcode.runtime import (
    CacheBust,
    ChildPlan,
    ChildText,
    Event,
    Message,
    PlanUpdated,
    RunStatus,
    TextDelta,
    Thinking,
    ThinkingDelta,
    ToolStarted,
    ToolSummary,
)
from pcode.shell import ShellPreview, result_projection
from pcode.tool_display import (
    COMMAND_TOOLS,
    assignment,
    command_error,
    delegation_detail,
    execution_mode,
    invocation,
    job_status,
    label,
    native_result_detail,
    native_result_projection,
    result_detail,
    stated_purpose,
    subject,
    target,
)
from pcode.turn import TurnContext

if TYPE_CHECKING:
    from pcode.live import AgentRuntime

_SHELL_EVENTS = (CommandStartedEvent, CommandOutputEvent, CommandFinishedEvent)


class EventTranslator:
    """Per-run state and one handler per upstream event kind."""

    def __init__(
        self,
        runtime: "AgentRuntime",
        context: TurnContext,
        plan_items: list[dict],
        *,
        plan_preview: StreamingPlanPreview | None,
        edit_preview: StreamingEditPreview | None,
    ) -> None:
        self.runtime = runtime
        self.context = context
        self.run_id = context.run_id
        # The plan as last published; a settled tool republishes it on change.
        self.plan_items = plan_items
        self.plan_preview = plan_preview
        self.edit_preview = edit_preview
        self.emitted_text = False
        # Running tools by call id: (name, args, monotonic start).
        self.tools: dict[str, tuple[str, dict, float]] = {}
        self.delegates: dict[str, ToolStarted] = {}
        self.child_tools: dict[str, ToolStarted] = {}
        self.delegation_ends: dict[str, DelegationEndEvent] = {}
        self.shell_preview = ShellPreview()
        self.shell_ends: dict[str, CommandFinishedEvent] = {}

    async def translate(self, event: Any) -> AsyncIterator[Event]:
        """Yield the pcode events for one upstream event, in display order."""
        if self.edit_preview is not None:
            for edit_update in self.edit_preview.update(event):
                yield edit_update
        if isinstance(event, FunctionToolResultEvent):
            # The only handler that awaits: it rereads the plan store.
            async for out in self.on_tool_result(event):
                yield out
        else:
            for out in self._dispatch(event):
                yield out
        if self.plan_preview is not None:
            if (update := self.plan_preview.update(event, self.plan_items)) is not None:
                yield update

    def _dispatch(self, event: Any) -> Iterator[Event]:
        if isinstance(event, CacheBustEvent):
            return self.on_cache_bust(event)
        if isinstance(event, FileChangeEvent):
            return self.on_file_change(event)
        if isinstance(event, DelegationStartEvent):
            return self.on_delegation_start(event)
        if isinstance(event, DelegationEndEvent):
            return self.on_delegation_end(event)
        if isinstance(event, ChildOutput):
            return self.on_child_output(event)
        if isinstance(event, ChildActivity):
            return self.on_child_activity(event)
        if isinstance(event, _SHELL_EVENTS):
            return self.on_shell(event)
        if isinstance(event, PartStartEvent):
            return self.on_part_start(event)
        if isinstance(event, PartDeltaEvent):
            return self.on_part_delta(event)
        if isinstance(event, PartEndEvent):
            return self.on_part_end(event)
        if isinstance(event, FunctionToolCallEvent):
            return self.on_tool_call(event)
        if isinstance(event, AgentRunResultEvent):
            return self.on_run_result(event)
        return iter(())

    def activity(self) -> RunStatus:
        tools = self.tools
        if not tools:
            return RunStatus("Waiting for model…")
        if len(tools) == 1:
            name, args, _ = next(iter(tools.values()))
            where = target(name, args)
            title = label(name) if name == "delegate_task" else name
            return RunStatus(f"Running {title}" + (f" · {where}" if where else "") + "…")
        names = ", ".join(label(item[0]) for item in list(tools.values())[:3])
        return RunStatus(f"Running {len(tools)} tools · {names}…")

    # Harness and pcode capability events

    def on_cache_bust(self, event: CacheBustEvent) -> Iterator[Event]:
        yield CacheBust(event.text)

    def on_file_change(self, event: FileChangeEvent) -> Iterator[Event]:
        yield event.change

    def on_delegation_start(self, event: DelegationStartEvent) -> Iterator[Event]:
        if start := self.delegates.get(event.tool_call_id):
            start = replace(start, activity="Waiting for model")
            self.delegates[event.tool_call_id] = start
            yield start

    def on_delegation_end(self, event: DelegationEndEvent) -> Iterator[Event]:
        self.delegation_ends[event.tool_call_id] = event
        # Deliberately not added to session totals: a child's *tokens*
        # already reach `result.usage` even under its own budget, so
        # adding `event.usage` here counts them twice. Only its
        # request count stays isolated, which is the point of the
        # budget. Tokens from an interrupted turn are a separate gap.
        # Timeouts/budget stops can leave a child's tool without a
        # result. Settle it before the parent resumes its tool loop.
        for call_id, child in list(self.child_tools.items()):
            if child.parent_call_id == event.tool_call_id:
                yield ToolSummary(
                    child.name,
                    child.detail + " → Interrupted",
                    failed=True,
                    call_id=call_id,
                    run_id=self.run_id,
                    outcome="interrupted",
                    parent_call_id=child.parent_call_id,
                )
                del self.child_tools[call_id]

    def on_child_output(self, event: ChildOutput) -> Iterator[Event]:
        if event.tool_call_id in self.delegates:
            yield ChildText(
                event.tool_call_id,
                event.text,
                thinking=event.thinking,
                start=event.start,
            )

    def on_child_activity(self, event: ChildActivity) -> Iterator[Event]:
        if start := self.delegates.get(event.tool_call_id):
            if event.activity != start.activity:
                start = replace(start, activity=event.activity)
                self.delegates[event.tool_call_id] = start
                yield start
            if event.plan is not None:
                yield ChildPlan(event.tool_call_id, event.plan)
            if event.child is not None:
                if isinstance(event.child, ToolStarted):
                    self.child_tools[event.child.call_id] = event.child
                else:
                    self.child_tools.pop(event.child.call_id, None)
                yield event.child

    def on_shell(
        self, event: CommandStartedEvent | CommandOutputEvent | CommandFinishedEvent
    ) -> Iterator[Event]:
        if isinstance(event, CommandFinishedEvent):
            self.shell_ends[event.tool_call_id] = event
        if (output := self.shell_preview.update(event)) is not None:
            yield output

    # Model response parts

    def on_part_start(self, event: PartStartEvent) -> Iterator[Event]:
        if isinstance(event.part, TextPart):
            yield TextDelta(event.part.content)
        elif isinstance(event.part, ThinkingPart):
            if event.part.content:
                yield ThinkingDelta(event.part.content)
            yield RunStatus("Thinking…")
        elif isinstance(event.part, NativeToolReturnPart):
            yield from self.on_native_tool_return(event.part)

    def on_native_tool_return(self, part: NativeToolReturnPart) -> Iterator[Event]:
        # Provider-executed tools (native web search/fetch) return
        # inside the response stream; there is no function event.
        name, args, started = self.tools.pop(part.tool_call_id, (part.tool_name, {}, monotonic()))
        detail, failed = native_result_detail(name, args, part.content, part.outcome)
        yield ToolSummary(
            name,
            detail,
            failed=failed,
            call_id=part.tool_call_id,
            result=capture(native_result_projection(part.content)),
            run_id=self.run_id,
            outcome=part.outcome if not failed else "error",
            elapsed_seconds=max(0, monotonic() - started),
        )
        yield self.activity()

    def on_part_delta(self, event: PartDeltaEvent) -> Iterator[Event]:
        if isinstance(event.delta, TextPartDelta):
            yield TextDelta(event.delta.content_delta)
        elif isinstance(event.delta, ThinkingPartDelta):
            if event.delta.content_delta:
                yield ThinkingDelta(event.delta.content_delta)
            yield RunStatus("Thinking…")

    def on_part_end(self, event: PartEndEvent) -> Iterator[Event]:
        if isinstance(event.part, TextPart) and event.part.content:
            self.emitted_text = True
            yield Message(event.part.content)
        elif isinstance(event.part, ThinkingPart) and event.part.content:
            yield Thinking(event.part.content)
        elif isinstance(event.part, NativeToolCallPart):
            yield from self.on_native_tool_call(event.part)

    def on_native_tool_call(self, part: NativeToolCallPart) -> Iterator[Event]:
        try:
            args = part.args_as_dict()
        except (ValueError, TypeError):
            args = {}
        self.tools[part.tool_call_id] = (part.tool_name, args, monotonic())
        agent, task = assignment(part.tool_name, args)
        yield ToolStarted(
            part.tool_name,
            target(part.tool_name, args),
            part.tool_call_id,
            arguments=capture(args if args else part.args),
            run_id=self.run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            agent=agent,
            task=task,
        )
        yield self.activity()

    # Function tools and the run's end

    def on_tool_call(self, event: FunctionToolCallEvent) -> Iterator[Event]:
        try:
            args = event.part.args_as_dict()
        except (ValueError, TypeError):
            args = {}
        self.tools[event.part.tool_call_id] = (event.part.tool_name, args, monotonic())
        agent, task = assignment(event.part.tool_name, args)
        command, purpose = subject(event.part.tool_name, args, self.runtime.jobs)
        start = ToolStarted(
            event.part.tool_name,
            target(event.part.tool_name, args),
            event.part.tool_call_id,
            arguments=capture(args if args else event.part.args),
            run_id=self.run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            process_id=capture(args.get("command_id", "")),
            command=command,
            purpose=purpose,
            execution=execution_mode(event.part.tool_name, args),
            agent=agent,
            task=task,
        )
        if event.part.tool_name == "delegate_task":
            self.delegates[event.part.tool_call_id] = start
        yield start
        yield self.activity()

    async def on_tool_result(self, event: FunctionToolResultEvent) -> AsyncIterator[Event]:
        name, args, started = self.tools.pop(
            event.tool_call_id,
            (event.part.tool_name or "tool", {}, monotonic()),
        )
        outcome = "retry" if isinstance(event.part, RetryPromptPart) else event.part.outcome
        detail, failed = result_detail(name, args, event.part.content, outcome)
        shell_end = self.shell_ends.pop(event.tool_call_id, None)
        if name == "shell" and shell_end is not None and outcome == "success":
            # The result's own job marker is authoritative: it is
            # written after the wait ends, while the event is a
            # snapshot the command can finish just after.
            status, failed = job_status(event.part.content)
            detail = target(name, args) + (f" → {status}" if status else "")
            if shell_end.truncated:
                detail += " · preview capped"
        self.delegates.pop(event.tool_call_id, None)
        end = self.delegation_ends.pop(event.tool_call_id, None)
        if end is not None:
            outcome = end.outcome
            detail, failed = delegation_detail(args, outcome)
        elif name == "delegate_task" and outcome == "success":
            # Harness returns max_calls refusals as normal strings,
            # with no lifecycle events because no child was launched.
            outcome = "not_started"
            detail = target(name, args) + " → Not started"
            failed = True
        # Read the store after every settled tool: covers granular,
        # batched, and future plan mutations without parsing results.
        items = [item.model_dump(mode="json") for item in await self.context.plan_store.get_items()]
        if items != self.plan_items:
            self.plan_items = items
            yield PlanUpdated(items)
        display_content = (
            result_projection(event.part.content, shell_end)
            if name == "shell"
            else event.part.content
        )
        yield ToolSummary(
            name,
            detail,
            failed=failed,
            call_id=event.tool_call_id,
            result=capture(display_content),
            run_id=self.run_id,
            outcome=outcome,
            process_id=(
                str(shell_end.pid)
                if shell_end is not None
                else match[1]
                if name == "start_command"
                and isinstance(event.part.content, str)
                and (match := re.search(r"^ID: (\w+)$", event.part.content, re.MULTILINE))
                else capture(args.get("command_id", ""))
            ),
            elapsed_seconds=max(0, monotonic() - started),
            command=invocation(name, args),
            purpose=stated_purpose(args),
            error=command_error(display_content) if failed and name in COMMAND_TOOLS else "",
        )
        yield self.activity()

    def on_run_result(self, event: AgentRunResultEvent) -> Iterator[Event]:
        result = event.result
        if result.output and not self.emitted_text:
            yield Message(str(result.output))
        # Full successful history. The outer persistence wrapper also
        # recovers settled tool-boundary snapshots after failures.
        self.context.history = result.all_messages()
        self.context.pending_shell = []
        self.runtime.turns += 1
