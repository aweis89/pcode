"""Opt-in sandboxed batching for the read-only tools, keeping mutations native.

Code mode collapses many dependent tool calls into one `run_code` snippet, which
pays off for investigation (fan out greps and reads, filter in Python). Tools
whose terminal display is the point -- edits with their diffs, plan updates, the
persistent shell, delegation -- stay native, so the transcript keeps showing what
changed rather than an opaque snippet that changed it.
"""

from typing import TYPE_CHECKING

from pcode.preferences import SETTINGS, load_preferences

if TYPE_CHECKING:
    from pydantic_ai_harness.code_mode import CodeMode

# Read-only lookups worth batching. Shell tools are already kept native by
# CodeMode itself; naming the sandboxed set explicitly also keeps MCP and future
# tools native until they are reviewed for it.
SANDBOXED_TOOLS = (
    "read_file",
    "list_files",
    "grep",
    "read_tool_result",
    "web_search",
    "get_page",
)


def code_mode_enabled() -> bool:
    preferences = load_preferences()
    return preferences.get("code_mode", SETTINGS["code_mode"].default) == "on"


def create_code_mode() -> "CodeMode | None":
    """Return the capability when enabled, or None to leave tool calling alone."""
    if not code_mode_enabled():
        return None
    # Imported here so the display modules can share SANDBOXED_TOOLS without
    # loading the Monty sandbox.
    from pydantic_ai_harness.code_mode import CodeMode

    return CodeMode(tools=list(SANDBOXED_TOOLS))
