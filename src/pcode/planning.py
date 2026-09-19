"""Make Harness's stable plan IDs visible when a plan is created."""

from pydantic_ai_harness.planning import Planning, render_plan

from pcode.meridian_reminders import append_reminder


class IdentifiedPlanning(Planning):
    """Keep upstream validation/storage, but disambiguate IDs from row numbers."""

    async def before_model_request(self, ctx, request_context):
        if request_context.model.system == "meridian" and self.inject:
            items = await self._read_plan(ctx)
            text = render_plan(items) if items else "No active plan."
            # Don't inject an empty plan until there is an earlier reminder to clear.
            if items or any(
                (m.metadata or {}).get("pcode_meridian_reminder", {}).get("kind") == "plan"
                for m in request_context.messages
            ):
                append_reminder(
                    request_context,
                    "plan",
                    text,
                    "<plan-reminder>\nCurrent plan (supersedes earlier plan reminders):\n"
                    f"{text}\n</plan-reminder>",
                )
        return request_context

    async def wrap_model_request(self, ctx, *, request_context, handler):
        if request_context.model.system == "meridian":
            return await handler(request_context)
        return await super().wrap_model_request(
            ctx, request_context=request_context, handler=handler
        )

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
