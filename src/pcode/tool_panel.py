"""Live tool activity: running calls only, since settled ones reach scrollback."""

from dataclasses import dataclass, field
from time import monotonic

from rich.text import Text

from pcode.runtime import ToolStarted, ToolSummary
from pcode.tool_display import PLAN_TOOLS, command_preview, label, plain

# Delegates outlive their own chatter, so they keep the panel's first rows.
DELEGATE = "delegate_task"
# A sub-agent's quick tools (a read, a grep) settle in milliseconds. Dropping
# their row the instant the result lands makes it flash unreadably and reflows
# the prompt, so a settled child row lingers long enough to be read.
CHILD_DWELL = 0.8
# The status row has the same problem, worse: a command that finishes in
# milliseconds appears and vanishes before it can be read, and the row snaps
# back to "Working…". The finished call keeps the row until it has been up this
# long, unless real work starts first.
STATUS_DWELL = 2.5
CHILD_INDENT = "    "
# A sub-agent's plan is a window around its active task, like the parent's, but
# shorter: several delegates share the panel with the parent's own tasks.
CHILD_PLAN_ROWS = 3
# Rows for tool work beside the parent's tasks, before any child plans.
TOOL_ROWS = 3
PLAN_ICONS = {
    "pending": "○",
    "completed": "✓",
    "cancelled": "–",
    "blocked": "!",
}


@dataclass
class ToolCall:
    event: ToolStarted
    started: float = field(default_factory=monotonic)
    settled: float | None = None

    @property
    def expired(self) -> bool:
        """A settled row has said its piece and no longer belongs on screen."""
        return self.settled is not None and monotonic() - self.settled >= CHILD_DWELL

    def line(self) -> str:
        """The call without a status icon; each surface supplies its own."""
        event = self.event
        # A stated purpose is what this row is for: the widget is the one place
        # that shows a job while it runs, when the command has not paid off yet.
        detail = (
            f"{event.purpose} · {command_preview(event.command)}"
            if event.command and event.purpose
            else command_preview(event.command)
            if event.command
            else plain(event.detail, limit=None)
        )
        state = f" · {plain(event.activity)}" if event.activity else ""
        # A settled call keeps the duration it finished with instead of ticking on.
        elapsed = (self.settled if self.settled is not None else monotonic()) - self.started
        return f"{label(event.name)}{state} · {elapsed:.1f}s · {detail}"


@dataclass
class ToolHistory:
    """Calls still in flight, oldest first. A result removes its call."""

    calls: list[ToolCall] = field(default_factory=list)
    # The last call to leave, kept only so the status row can hold it.
    recent: ToolCall | None = None
    # Each running delegate's plan, keyed by its call id; it leaves with the delegate.
    plans: dict[str, list[dict]] = field(default_factory=dict)

    def record_plan(self, call_id: str, items: list[dict]) -> None:
        if any(c.event.call_id == call_id for c in self.calls):
            self.plans[call_id] = items

    def record(self, event: ToolStarted | ToolSummary) -> None:
        # Planning operations have their own panel. Settled calls leave the
        # live view regardless of whether they need a scrollback entry.
        if event.name in PLAN_TOOLS:
            return
        self.prune()
        existing = next(
            (c for c in self.calls if event.call_id and c.event.call_id == event.call_id), None
        )
        if isinstance(event, ToolSummary):
            if existing is None:
                return
            # A child's row is the only trace of the sub-agent's step, so let it
            # dwell; anything else leaves as soon as it settles.
            if existing.event.parent_call_id and existing.settled is None:
                existing.settled = monotonic()
            else:
                existing.settled = monotonic()
                self.recent = existing
                self._drop(existing)
        elif existing is not None:
            # A restated start carries fresh progress, not a new invocation.
            existing.event = event
        else:
            self.calls.append(ToolCall(event))

    def _drop(self, call: ToolCall) -> None:
        """A settled call leaves, and takes any child row still waiting out its dwell."""
        self.plans.pop(call.event.call_id, None)
        self.calls = [
            c for c in self.calls if c is not call and c.event.parent_call_id != call.event.call_id
        ]

    def prune(self) -> None:
        """Forget dwelt-out rows, so the animation loop can stop once nothing runs."""
        self.calls = [c for c in self.calls if not c.expired]

    def clear(self) -> None:
        self.calls.clear()
        self.plans.clear()
        self.recent = None

    @property
    def visible(self) -> list[ToolCall]:
        """Calls worth a row: in flight, or settled within the dwell window."""
        return [c for c in self.calls if not c.expired]

    @property
    def active(self) -> ToolCall | None:
        """What the status row above the tasks reports.

        The newest running call, or else the one that just finished, for as
        long as its dwell lasts. Anything that starts meanwhile wins the row:
        holding a stale line over live work would be the worse lie.
        """
        running = next((c for c in reversed(self.visible) if c.settled is None), None)
        if running is not None:
            return running
        held = self.recent
        if held is not None and monotonic() - held.started < STATUS_DWELL:
            return held
        self.recent = None
        return None

    @property
    def background(self) -> list[ToolCall]:
        """Everything the status row does not already show."""
        active = self.active
        return [c for c in self.visible if c is not active]

    def _delegates(self) -> list[ToolCall]:
        """Running delegates that get a panel row.

        Those beside the status row, plus any with a plan even while it holds
        the status row: otherwise its tasks would vanish every time the
        sub-agent went back to the model and the delegate took the row back.
        """
        active = self.active
        return [
            c
            for c in self.visible
            if c.event.name == DELEGATE
            and c.settled is None
            and (c is not active or self.plans.get(c.event.call_id))
        ]

    def plan_rows(self) -> int:
        """Rows the running delegates' plans would fill, before any budget."""
        return sum(
            min(CHILD_PLAN_ROWS, len(self.plans.get(c.event.call_id, [])))
            for c in self._delegates()
        )

    def wanted(self) -> int:
        """Rows every call and child plan would fill, before any budget."""
        others = [c for c in self.background if c.event.name != DELEGATE]
        return len(self._delegates()) + len(others) + self.plan_rows()

    def rows(self, count: int, *, nested: bool = False, icon: str = "⟳"):
        """Delegates first: a running sub-agent must stay addressable and visible.

        Beneath each goes its plan, then its own calls, nested under its active
        task the way the parent's calls sit under the parent's. Both are bounded
        so several delegates cannot crowd each other out.
        """
        calls = self.background
        base = "    " if nested else ""
        delegates = self._delegates()[:count]
        remaining = count - len(delegates)
        lines = []
        for parent in delegates:
            lines.append(_call_row(parent, base))
            plan = self.plans.get(parent.event.call_id, [])
            steps, active = plan_window(plan, min(CHILD_PLAN_ROWS, len(plan), remaining))
            remaining -= len(steps)
            children = [c for c in calls if c.event.parent_call_id == parent.event.call_id]
            children = children[-min(2, remaining) :] if remaining else []
            remaining -= len(children)
            for index in steps:
                lines.append(plan_row(plan[index], icon, base + CHILD_INDENT))
                if index == active:
                    lines.extend(_call_row(c, base + CHILD_INDENT * 2) for c in children)
            if active is None or active not in steps:
                lines.extend(_call_row(c, base + CHILD_INDENT) for c in children)
        if remaining:
            other = [c for c in calls if not c.event.parent_call_id and c.event.name != DELEGATE]
            lines.extend(_call_row(c, base) for c in other[:remaining])
        return lines


def _call_row(call: ToolCall, indent: str) -> tuple[str, str]:
    done = call.settled is not None
    style = "class:plan" if done else "class:plan.active"
    return style, f"{indent}{'✓' if done else '⟳'} {call.line()}"


def plan_window(items: list[dict], count: int) -> tuple[range, int | None]:
    """The `count` steps worth showing, centred on the active one, and its index."""
    active = next((i for i, item in enumerate(items) if item["status"] == "in_progress"), None)
    anchor = active if active is not None else 0
    start = min(max(0, anchor - count // 2), max(0, len(items) - count))
    return range(start, start + count), active


def plan_row(item: dict, active_icon: str, indent: str = "") -> tuple[str, str]:
    status = item["status"]
    style = "class:plan.active" if status == "in_progress" else "class:plan"
    icon = active_icon if status == "in_progress" else PLAN_ICONS.get(status, "○")
    return style, f"{indent}{icon} {plain(item['content'], limit=None)}"


def task_panel_rows(items: list[dict], tools: ToolHistory, budget: int, active_icon: str):
    """A bounded task viewport, with any concurrent tool work below the active task.

    The newest call lives on the status row instead, so this only shows work
    running alongside it: delegates (with their own plans) and other parallel
    calls. Keep at least one task visible, even on short panes, and never add
    headers or empty rows.

    Rendering is also when dwelt-out rows are forgotten, so the animation loop
    stops once the last settled row has left.
    """
    tools.prune()
    if budget <= 0:
        return []
    tool_count = min(TOOL_ROWS + tools.plan_rows(), tools.wanted(), max(0, budget - bool(items)))
    steps, active = plan_window(items, min(5, len(items), budget - tool_count))
    lines = []
    for index in steps:
        lines.append(plan_row(items[index], active_icon))
        if index == active:
            lines.extend(tools.rows(tool_count, nested=True, icon=active_icon))
    if active is None or active not in steps:
        lines.extend(tools.rows(tool_count, icon=active_icon))
    return lines


def panel_fragments(lines: list[tuple[str, str]], width: int):
    """Clip by terminal cells before prompt_toolkit renders non-wrapping rows."""
    fragments = []
    for index, (style, line) in enumerate(lines):
        text = Text(plain(line, limit=None))
        text.truncate(max(1, width), overflow="ellipsis")
        fragments.append((style, ("\n" if index else "") + text.plain))
    return fragments
