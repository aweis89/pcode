"""Make Harness's stable plan IDs visible when a plan is created."""

from pydantic_ai_harness.planning import Planning


class IdentifiedPlanning(Planning):
    """Keep upstream validation/storage, but disambiguate IDs from row numbers."""

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
        if call.tool_name == "write_plan" and isinstance(result, str) and result.startswith(
            "Plan updated:"
        ):
            items = await self.resolve_store(ctx).get_items()
            if items:
                result += "\n\nStable task IDs (not row numbers):\n" + "\n".join(
                    f"{index}. task_id={item.id}" for index, item in enumerate(items, 1)
                )
        return result
