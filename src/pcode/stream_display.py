"""Presentation-only event routing, shared by live streaming and offline benchmarks."""

from pcode.runtime import (
    CacheBust,
    ChildPlan,
    CommandOutput,
    EditCompleted,
    EditPreview,
    Message,
    PlanPreview,
    PlanUpdated,
    RunStatus,
    TextDelta,
    Thinking,
    ThinkingDelta,
    ToolStarted,
    ToolSummary,
)


def present_events(events, *, activity, transcript, edits) -> None:
    """Route live tool activity separately from permanent transcript writes."""
    for event in events:
        if isinstance(event, EditPreview):
            activity.edit_previews.pop(event.call_id, None)
            if event.path:
                activity.edit_previews[event.call_id] = event
        elif isinstance(event, EditCompleted):
            edits.append(event)
            transcript.edit(event)
        elif isinstance(event, CommandOutput):
            activity.command_outputs.pop(event.call_id, None)
            activity.command_outputs[event.call_id] = event
        elif isinstance(event, (ToolStarted, ToolSummary)):
            activity.tools.record(event)
            if transcript.output is not None:
                transcript.output.app.invalidate()
            # The adapter's failed flag includes non-zero exits and tool retries.
            if isinstance(event, ToolSummary):
                activity.command_outputs.pop(event.call_id, None)
                transcript.tool_result(event)
        else:
            transcript.events((event,))


def present_stream_event(event, *, output, transcript, activity, present) -> None:
    """Apply one display event without contacting a model or executing a tool."""
    if isinstance(event, ThinkingDelta):
        output.finish()
        output.thinking_delta(event.text)
    elif isinstance(event, Thinking):
        output.finish_thinking(event.text)
    elif isinstance(event, TextDelta):
        output.finish_thinking()
        output.delta(event.text)
        activity.status = "Responding…"
    elif isinstance(event, CacheBust):
        output.finish_thinking()
        output.finish()
        transcript.events((event,))
    elif isinstance(event, EditCompleted):
        output.finish_thinking()
        output.finish()
        present((event,))
    elif isinstance(event, (CommandOutput, EditPreview)):
        present((event,))
    elif isinstance(event, RunStatus):
        activity.status = event.text
    elif isinstance(event, PlanUpdated):
        activity.plan = event.items
    elif isinstance(event, PlanPreview):
        activity.plan_preview = event.items
    elif isinstance(event, ChildPlan):
        activity.tools.record_plan(event.call_id, event.items)
    elif isinstance(event, (ToolStarted, ToolSummary)):
        output.finish_thinking()
        # Settled calls land in scrollback, so prose must be committed first.
        # Suppressed calls, such as successful planning, do not interrupt it.
        if transcript.writes_tool_result(event):
            output.finish()
        present((event,))
    elif isinstance(event, Message):
        output.finish(event.markdown)
    else:
        output.finish()
        transcript.events((event,))
    output.app.invalidate()
