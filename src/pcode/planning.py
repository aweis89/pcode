"""Harness planning, tuned for a plan the user watches as a live checklist."""

from dataclasses import dataclass

from pydantic_ai import CapabilityEvent
from pydantic_ai_harness.planning import Planning, render_plan

from pcode.meridian_reminders import PLAN_TAG, append_reminder, last_reminder
from pcode.tool_display import PLAN_TOOLS

# Replaces Harness's default guidance. That text keys the plan on "multi-step
# work" and never says anyone sees it, so the model treats a plan as overhead
# and argues most tasks out of it, leaving the plan panel empty. This names the
# reader, sets a threshold the model can judge before it starts (lookups are
# out however many reads they take), asks for outcome-level steps so progress
# is cheap to report, and says what the plan should look like when a turn ends.
GUIDANCE = (
    "You have a planning tool, `write_plan`. The user sees the plan as a live checklist "
    "of what you are doing, what is done, and what is left. "
    "Keep it truthful: one step `in_progress` while work is underway, "
    "and when a step finishes, one `update_task_statuses` call that completes it "
    "and starts the next. Use `add_task` for a step you discover midway; "
    "use `write_plan` only to create or restructure the plan, and pass the full "
    "plan when you do. Before your final reply, every step should be `completed` or "
    "`cancelled`, unless you are stopping to ask the user; then leave the rest as it is."
)

ID_GUIDANCE = (
    " Plan row numbers are display positions, not task IDs. For granular updates, use "
    "the exact stable IDs returned by write_plan or read_plan; never guess IDs from row "
    "numbers. Preserve those IDs when rewriting a plan."
)

# Harness's description tells the model to call write_plan "first for multi-step
# work, then again as you start and finish steps", which repeats the narrow
# trigger GUIDANCE drops and steers routine progress away from the cheaper
# status tools.
WRITE_PLAN_DESCRIPTION = (
    "Create or replace the entire plan. Pass the whole ordered list every time -- "
    "including steps that are unchanged, completed, or cancelled -- so there are no "
    "indices to track. Use it to create the plan or restructure it; report routine "
    "progress with `update_task_statuses` instead. Keep one step `in_progress` while "
    "work is underway."
)


@dataclass(kw_only=True)
class PlanSnapshot(CapabilityEvent, namespace="pcode_planning", name="snapshot"):
    """The whole plan after a planning call, announced on the run's own event stream.

    A sub-agent's plan lives in a store private to its run. Announcing it lets
    the parent's display show it without reaching into, or replacing, that store.
    """

    items: list[dict]


@dataclass
class IdentifiedPlanning(Planning):
    """Keep upstream validation/storage; change what the model is told about the plan.

    GUIDANCE names the full core toolset. A caller narrowing `tools` or enabling
    subtasks should pass its own `guidance`, as upstream's `tools` docs already advise.
    """

    def __post_init__(self):
        # An explicit `descriptions` still wins per tool.
        self.descriptions = {"write_plan": WRITE_PLAN_DESCRIPTION, **(self.descriptions or {})}

    async def before_model_request(self, ctx, request_context):
        if self.inject:
            items = await self._read_plan(ctx)
            text = render_plan(items) if items else "No active plan."
            # Don't inject an empty plan until there is an earlier reminder to clear.
            if items or last_reminder(request_context.messages, PLAN_TAG):
                append_reminder(
                    request_context,
                    PLAN_TAG,
                    f"{PLAN_TAG}\nCurrent plan (supersedes earlier plan reminders):\n"
                    f"{text}\n</plan-reminder>",
                )
        return request_context

    async def wrap_model_request(self, ctx, *, request_context, handler):
        # Upstream appends an ephemeral reminder here. Removing it on the next
        # request invalidates the newly cached tail. Persist changes in the
        # before hook instead, without moving explicit cache markers in history.
        return await handler(request_context)

    def get_instructions(self):
        # As upstream: None means the default, "" drops it. The ID note always stays.
        guidance = GUIDANCE if self.guidance is None else self.guidance
        return (guidance + ID_GUIDANCE).strip()

    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        result = await super().after_tool_execute(
            ctx, call=call, tool_def=tool_def, args=args, result=result
        )
        if call.tool_name in PLAN_TOOLS:
            items = await self.resolve_store(ctx).get_items()
            await ctx.emit(PlanSnapshot(items=[item.model_dump(mode="json") for item in items]))
        if (
            call.tool_name == "write_plan"
            and isinstance(result, str)
            and result.startswith("Plan updated:")
        ):
            items = await self.resolve_store(ctx).get_items()
            if items:
                result += "\n\nStable task IDs (not row numbers):\n" + "\n".join(
                    f"{index}. task_id={item.id}" for index, item in enumerate(items, 1)
                )
        return result
