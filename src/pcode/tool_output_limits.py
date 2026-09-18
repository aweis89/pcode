"""Configure Harness's production-time tool reduction, independently of compaction."""

import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from pydantic_ai.messages import ToolReturn
from pydantic_ai_harness.tool_output_limits import (
    Band,
    LocalFileStore,
    Spill,
    ToolOutputLimits,
    Truncate,
    TruncationStrategy,
    indented_json,
)

from pcode.preferences import SETTINGS, load_preferences
from pcode.shell import REDUCED_SHELL_OUTPUT


def tool_results_path() -> Path:
    """Stable across workspaces and restarts so saved spill handles remain readable."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "tool-results"


class CodingToolOutputLimits(ToolOutputLimits):
    """Keep the persistent shell's control envelope outside the reduction budget."""

    def get_instructions(self):
        # Harness pages by line and caps reads at 50k chars, so one very long line
        # cannot be fully retrieved that way. Both agents have unrestricted shell.
        return (
            "Large tool results may be stored with a read_tool_result handle. "
            "Read only the slices needed. If a single long line hits its output cap, "
            f"use shell to read a character range from {tool_results_path()}/<handle>."
        )

    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        if call.tool_name != "shell" or not isinstance(result, str):
            return await super().after_tool_execute(
                ctx, call=call, tool_def=tool_def, args=args, result=result
            )
        output, separator, handles = result.rpartition("\nPID: ")
        if not separator:
            return await super().after_tool_execute(
                ctx, call=call, tool_def=tool_def, args=args, result=result
            )
        reduced = await super().after_tool_execute(
            ctx, call=call, tool_def=tool_def, args=args, result=output
        )
        if reduced == output:
            return result
        value = reduced.return_value if isinstance(reduced, ToolReturn) else reduced
        # The marker also tells display projection that clipping lost redaction
        # context, including for delegated calls without CommandFinishedEvent.
        value = f"{REDUCED_SHELL_OUTPUT}\n{value}{separator}{handles}"
        return replace(reduced, return_value=value) if isinstance(reduced, ToolReturn) else value


def create_tool_output_limits() -> ToolOutputLimits:
    """Snapshot saved settings; all reduction and retrieval behavior stays upstream."""
    preferences = load_preferences()

    def value(suffix: str) -> str:
        key = f"tool_output_{suffix}"
        result = preferences.get(key, SETTINGS[key].default)
        assert result is not None
        return result

    mode = value("mode")
    threshold = int(value("threshold"))
    truncate = Truncate(
        max_chars=int(value("max_chars")),
        strategy=TruncationStrategy(value("strategy")),
    )
    action = (
        Spill(preview_chars=int(value("preview_chars")), then=truncate)
        if mode == "spill"
        else truncate
    )
    retention = int(value("retention_hours"))
    return CodingToolOutputLimits(
        bands=[] if mode == "off" else [Band(over=threshold, action=action)],
        store=LocalFileStore(
            base_dir=tool_results_path(),
            cleanup_after=timedelta(hours=retention) if retention else None,
        ),
        # Structured results (e.g. MCP) need real lines for useful read-back paging.
        serializer=indented_json,
        # Keep read_tool_result even when reduction is off or truncate-only:
        # resumed history can still contain handles produced in spill mode.
    )
