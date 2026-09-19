"""Live tool activity: running calls only, since settled ones reach scrollback."""

from dataclasses import dataclass, field
from time import monotonic

from rich.text import Text

from pcode.runtime import ToolStarted, ToolSummary
from pcode.tool_display import PLAN_TOOLS, command_preview, label, plain

# Delegates outlive their own chatter, so they keep the panel's first rows.
DELEGATE = "delegate_task"


@dataclass
class ToolCall:
    event: ToolStarted
    started: float = field(default_factory=monotonic)

    def line(self) -> str:
        """The call without a status icon; each surface supplies its own."""
        event = self.event
        detail = (
            command_preview(event.command) if event.command else plain(event.detail, limit=None)
        )
        state = f" · {plain(event.activity)}" if event.activity else ""
        return f"{label(event.name)}{state} · {monotonic() - self.started:.1f}s · {detail}"


@dataclass
class ToolHistory:
    """Calls still in flight, oldest first. A result removes its call."""

    calls: list[ToolCall] = field(default_factory=list)

    def record(self, event: ToolStarted | ToolSummary) -> None:
        # Planning operations have their own panel, and every settled call is
        # written to scrollback, so neither belongs in the live view.
        if event.name in PLAN_TOOLS:
            return
        existing = next(
            (c for c in self.calls if event.call_id and c.event.call_id == event.call_id), None
        )
        if isinstance(event, ToolSummary):
            if existing is not None:
                self.calls.remove(existing)
        elif existing is not None:
            # A restated start carries fresh progress, not a new invocation.
            existing.event = event
        else:
            self.calls.append(ToolCall(event))

    def clear(self) -> None:
        self.calls.clear()

    @property
    def active(self) -> ToolCall | None:
        """The newest call: what the status row above the tasks reports."""
        return self.calls[-1] if self.calls else None

    @property
    def background(self) -> list[ToolCall]:
        """Everything the status row does not already show."""
        return self.calls[:-1]

    def rows(self, count: int, *, nested: bool = False):
        """Delegates first: a running sub-agent must stay addressable and visible.

        Its own chatter is bounded so several delegates cannot crowd each other out.
        """
        calls = self.background
        delegates = [c for c in calls if c.event.name == DELEGATE]
        visible = delegates[:count]
        remaining = count - len(visible)
        for parent in delegates[:count]:
            children = [c for c in calls if c.event.parent_call_id == parent.event.call_id]
            children = children[-min(2, remaining) :] if remaining else []
            index = next(i for i, c in enumerate(visible) if c is parent) + 1
            visible[index:index] = children
            remaining -= len(children)
        if remaining:
            other = [c for c in calls if not c.event.parent_call_id and c.event.name != DELEGATE]
            visible.extend(other[:remaining])
        lines = []
        for call in visible:
            indent = ("    " if nested else "") + ("    " if call.event.parent_call_id else "")
            lines.append(("class:plan.active", f"{indent}⟳ {call.line()}"))
        return lines


def task_panel_rows(items: list[dict], tools: ToolHistory, budget: int, active_icon: str):
    """A bounded task viewport, with any concurrent tool work below the active task.

    The newest call lives on the status row instead, so this only shows work
    running alongside it: delegates and other parallel calls. Keep at least one
    task visible, even on short panes, and never add headers or empty rows.
    """
    if budget <= 0:
        return []
    tool_count = min(3, len(tools.background), max(0, budget - bool(items)))
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
        lines.append((style, f"{icons.get(status, '○')} {content}"))
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
