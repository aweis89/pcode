"""Bounded tool activity, separate from the conversation transcript."""

from dataclasses import dataclass, field
from time import monotonic

from rich.text import Text

from pcode.runtime import ToolStarted, ToolSummary
from pcode.tool_display import PLAN_TOOLS, command_preview, label, plain


@dataclass
class ToolCall:
    event: ToolStarted | ToolSummary
    started: float = field(default_factory=monotonic)
    interrupted: bool = False

    @property
    def running(self) -> bool:
        return isinstance(self.event, ToolStarted) and not self.interrupted

    def line(self) -> str:
        event = self.event
        icon = "⟳" if self.running else "–" if self.interrupted else "!" if event.failed else "✓"
        elapsed = (
            monotonic() - self.started if self.running else getattr(event, "elapsed_seconds", None)
        )
        timing = f" · {elapsed:.1f}s" if elapsed is not None else ""
        detail = (
            command_preview(event.command) if event.command else plain(event.detail, limit=None)
        )
        state = " · interrupted" if self.interrupted else ""
        # Keep failures visible even when the command itself consumes the row.
        name = label(event.name) + (" failed" if getattr(event, "failed", False) else "")
        return f"  {icon} {name}{state}{timing} · {detail}"


@dataclass
class ToolHistory:
    calls: list[ToolCall] = field(default_factory=list)
    # Only unfinished calls live here. Their identities outlast eviction from
    # the ten-row history, so a late result cannot count as a new invocation.
    _running: dict[str, ToolCall] = field(default_factory=dict, repr=False)

    def record(self, event: ToolStarted | ToolSummary) -> None:
        # Successful planning operations already have their own panel. Failed
        # operations still need an inspectable error, rather than disappearing.
        if event.name in PLAN_TOOLS and not getattr(event, "failed", False):
            return
        call = self._running.get(event.call_id) if event.call_id else None
        if isinstance(event, ToolSummary) and call is None:
            call = next(
                (
                    c
                    for c in reversed(self.calls)
                    if event.call_id and c.event.call_id == event.call_id
                ),
                None,
            )
        if call is not None:
            call.event = event
            call.interrupted = False
        else:
            call = ToolCall(event)
            self.calls.append(call)
            del self.calls[:-10]
        if event.call_id:
            if isinstance(event, ToolStarted):
                self._running[event.call_id] = call
            else:
                self._running.pop(event.call_id, None)

    def interrupt_running(self) -> None:
        for call in [*self.calls, *self._running.values()]:
            if call.running:
                call.interrupted = True
        self._running.clear()

    def clear(self) -> None:
        self.calls.clear()
        self._running.clear()

    def rows(self, count: int, *, nested: bool = False):
        visible = self.calls[-count:] if count else []
        lines = []
        for call in visible:
            style = "class:tool.failed" if getattr(call.event, "failed", False) else "class:plan"
            if call.running:
                style = "class:plan.active"
            lines.append((style, ("    " if nested else "") + call.line()))
        return lines


def task_panel_rows(items: list[dict], tools: ToolHistory, budget: int, active_icon: str):
    """A bounded task viewport with recent tools directly below the active task.

    Tools are a rolling view of recent activity, not persisted task ownership.
    Without an active task they appear at root indentation after the task rows.
    Keep at least one task visible, even on short panes, and never add headers
    or empty placeholder rows.
    """
    if budget <= 0:
        return []
    tool_count = min(5, len(tools.calls), max(0, budget - bool(items)))
    task_count = min(5, len(items), budget - tool_count)
    active = next((i for i, item in enumerate(items) if item["status"] == "in_progress"), None)
    anchor = active if active is not None else 0
    start = min(max(0, anchor - task_count // 2), max(0, len(items) - task_count))
    icons = {
        "pending": "○",
        "in_progress": active_icon,
        "completed": "✓",
        "cancelled": "–",
        "blocked": "!",
    }
    lines = []
    for index in range(start, start + task_count):
        item = items[index]
        status = item["status"]
        style = "class:plan.active" if status == "in_progress" else "class:plan"
        content = plain(item["content"], limit=None)
        lines.append((style, f"  {icons.get(status, '○')} {content}"))
        if index == active:
            lines.extend(tools.rows(tool_count, nested=True))
    if active is None:
        lines.extend(tools.rows(tool_count))
    return lines


def panel_fragments(lines: list[tuple[str, str]], width: int):
    """Clip by terminal cells before prompt_toolkit renders non-wrapping rows."""
    fragments = []
    for index, (style, line) in enumerate(lines):
        text = Text(plain(line, limit=None))
        text.truncate(max(1, width), overflow="ellipsis")
        fragments.append((style, ("\n" if index else "") + text.plain))
    return fragments
