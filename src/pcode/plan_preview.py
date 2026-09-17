"""Ephemeral task-pane projections of streamed planning calls.

These never mutate Harness's store. Complete strings/fields can be displayed while
JSON is still arriving; validation, IDs, and persistence belong to the real tools.
"""

from copy import deepcopy

from pydantic import ValidationError
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    ToolCallPart,
    ToolCallPartDelta,
)
from pydantic_ai_harness.planning import PlanItem
from pydantic_core import from_json

from pcode.runtime import PlanPreview

PLANNING_MUTATIONS = {
    "write_plan",
    "add_task",
    "update_task_status",
    "update_task_statuses",
    "remove_task",
}


def project_plan(items: list[dict], part: ToolCallPart) -> list[dict]:
    """Best-effort display only; malformed/incomplete fields leave the plan alone."""
    try:
        args = from_json(part.args, allow_partial=True) if isinstance(part.args, str) else part.args
    except ValueError:
        return items
    if not isinstance(args, dict):
        return items

    def row(value, index):
        if not isinstance(value, dict):
            return None
        try:
            return PlanItem.model_validate(
                {"id": f"preview:{part.tool_call_id}:{index}", **value}
            ).model_dump(mode="json")
        except ValidationError:
            return None

    if part.tool_name == "write_plan":
        values = args.get("items")
        if isinstance(values, list):
            rows = [item for i, value in enumerate(values) if (item := row(value, i)) is not None]
            if rows:
                return rows
            # An unfinished `items: [` is not an instruction to clear the pane.
            if values == []:
                try:
                    part.args_as_dict(raise_if_invalid=True)
                except (ValueError, TypeError):
                    return items
                return []
    elif part.tool_name == "add_task":
        if (item := row(args, 0)) is not None:
            return [*items, item]
    elif part.tool_name in {"update_task_status", "update_task_statuses"}:
        updates = args.get("updates") if part.tool_name == "update_task_statuses" else [args]
        if isinstance(updates, list):
            items = deepcopy(items)
            for update in updates:
                if not isinstance(update, dict):
                    continue
                for i, item in enumerate(items):
                    if item["id"] == update.get("task_id"):
                        changed = row({**item, "status": update.get("status")}, i)
                        if changed is not None:
                            items[i] = changed
    elif part.tool_name == "remove_task":
        return [item for item in items if item["id"] != args.get("task_id")]
    return items


class StreamingPlanPreview:
    def __init__(self):
        self.parts: dict[int, ToolCallPart] = {}
        self.items: list[dict] | None = None
        self.executing = False
        self.confirmed: list[dict] = []

    def update(self, event, confirmed: list[dict]) -> PlanPreview | None:
        store_changed = confirmed != self.confirmed
        self.confirmed = confirmed
        if isinstance(event, PartStartEvent):
            if self.executing or event.index == 0:
                self.parts.clear()
                self.executing = False
            if isinstance(event.part, ToolCallPart):
                self.parts[event.index] = event.part
        elif isinstance(event, PartDeltaEvent):
            if isinstance(event.delta, ToolCallPartDelta) and event.index in self.parts:
                self.parts[event.index] = event.delta.apply(self.parts[event.index])
        elif isinstance(event, PartEndEvent) and isinstance(event.part, ToolCallPart):
            self.parts[event.index] = event.part
        elif isinstance(event, FunctionToolCallEvent):
            self.executing = True
        elif isinstance(event, FunctionToolResultEvent):
            if store_changed:
                # The shared store can already contain sibling mutations whose
                # result events have not arrived. Do not replay their arguments
                # over that snapshot (e.g. duplicating concurrent add_task calls).
                self.parts.clear()
            else:
                self.parts = {
                    index: part
                    for index, part in self.parts.items()
                    if part.tool_call_id != event.tool_call_id
                }
        else:
            return None

        items = confirmed
        for part in self.parts.values():
            if part.tool_name in PLANNING_MUTATIONS:
                items = project_plan(items, part)
        preview = items if items != confirmed else None
        if preview != self.items:
            self.items = preview
            return PlanPreview(preview)
        return None
