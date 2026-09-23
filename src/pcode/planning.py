"""Make Harness's stable plan IDs visible when a plan is created."""

from dataclasses import dataclass

from pydantic_ai import CapabilityEvent
from pydantic_ai_harness.planning import Planning, render_plan

from pcode.meridian_reminders import PLAN_TAG, append_reminder, last_reminder
from pcode.tool_display import PLAN_TOOLS


@dataclass(kw_only=True)
class PlanSnapshot(CapabilityEvent, namespace="pcode_planning", name="snapshot"):
    """The whole plan after a planning call, announced on the run's own event stream.

    A sub-agent's plan lives in a store private to its run. Announcing it lets
    the parent's display show it without reaching into, or replacing, that store.
    """

    items: list[dict]


class IdentifiedPlanning(Planning):
    """Keep upstream validation/storage, but disambiguate IDs from row numbers."""

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
        guidance = super().get_instructions()
        return (guidance or "") + (
            " Plan row numbers are display positions, not task IDs. For granular "
            "updates, use the exact stable IDs returned by write_plan or read_plan; "
            "never guess IDs from row numbers. Preserve those IDs when rewriting a plan."
        )

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
