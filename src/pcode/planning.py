"""Harness planning, tuned for a plan the user watches as a live checklist."""

from dataclasses import dataclass

from pydantic_ai import CapabilityEvent
from pydantic_ai_harness.planning import Planning

from pcode.tool_display import PLAN_TOOLS

# Replace Harness's "multi-step work" threshold with default use for visible
# progress, including small tasks and investigations. Say to create the plan
# early: maintenance rules alone leave creating one optional.
GUIDANCE = (
    "Use `write_plan` by default when working on a request. The plan is the user's "
    "live checklist of what you are doing, what is done, and what is left—not just "
    "a tool for organizing complex work. Small tasks and investigations count, even "
    "if the plan has only one step. Create it early and revise it as you learn; "
    "you do not need to know the whole solution first. "
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

    # Tool results already report changes; read_plan retrieves state when needed.
    inject: bool = False

    def __post_init__(self):
        # An explicit `descriptions` still wins per tool.
        self.descriptions = {"write_plan": WRITE_PLAN_DESCRIPTION, **(self.descriptions or {})}

    @classmethod
    def from_spec(cls, *, inject: bool = False, **kwargs):
        return super().from_spec(inject=inject, **kwargs)

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
