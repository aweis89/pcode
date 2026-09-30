"""Live tool activity: running calls only, since settled ones reach scrollback."""

from dataclasses import dataclass, field
from time import monotonic

from rich.text import Text

from pcode.runtime import ToolStarted, ToolSummary
from pcode.tool_display import JOB_HANDLE_TOOLS, PLAN_TOOLS, command_preview, label, plain

# Delegates outlive their own chatter, so they keep the panel's first rows.
DELEGATE = "delegate_task"
# Marks a row as a sub-agent rather than a tool. One terminal cell wide in
# common fonts, unlike emoji, so the panel's width math still holds.
AGENT_ICON = "✦"
# Marks a wait on a job an earlier call started, so it never reads as a fresh
# run of the command it names. One cell wide, for the same reason.
WAIT_ICON = "⧗"
# The status row has the same problem, worse: a command that finishes in
# milliseconds appears and vanishes before it can be read, and a burst of them
# strobes. A finished call keeps the row, marked done, for this long after it
# settles, unless real work starts first. The tally on the same row is the
# lasting record, so this only has to stop the flicker.
STATUS_DWELL = 0.6
# A sub-agent's plan is a window around its active task, like the parent's, but
# shorter: several delegates share the panel with the parent's own tasks.
CHILD_PLAN_ROWS = 3
# Rows for tool work beside the parent's tasks, before any child plans.
TOOL_ROWS = 3
# The parent's task window at the default height. With `tasks_max_height`
# set, the tasks fill whatever budget the tools leave instead.
TASK_ROWS = 5
PLAN_ICONS = {
    "pending": "○",
    "completed": "✓",
    "cancelled": "–",
    "blocked": "!",
}


@dataclass
class _PanelNode:
    row: tuple[str, str]
    children: list["_PanelNode"] = field(default_factory=list)


def _tree_rows(nodes: list[_PanelNode], prefix: str | None = None) -> list[tuple[str, str]]:
    """Draw guides for the visible tree, leaving unparented roots undecorated."""
    rows = []
    for index, node in enumerate(nodes):
        last = index == len(nodes) - 1
        style, text = node.row
        branch = "" if prefix is None else prefix + ("└── " if last else "├── ")
        rows.append((style, branch + text))
        stem = "" if prefix is None else prefix + ("    " if last else "│   ")
        rows.extend(_tree_rows(node.children, stem))
    return rows


@dataclass
class ToolCall:
    event: ToolStarted
    started: float = field(default_factory=monotonic)
    settled: float | None = None
    failed: bool = False

    @property
    def elapsed(self) -> float:
        """Seconds so far; a settled call keeps the duration it finished with."""
        return (self.settled if self.settled is not None else monotonic()) - self.started

    def line(self, *, timed: bool = True) -> str:
        """The call without a status icon; each surface supplies its own.

        `timed=False` leaves the duration out, for the status row, which
        keeps its clock in a fixed column of its own.
        """
        event = self.event
        elapsed = self.elapsed if timed else None
        if event.name == DELEGATE:
            return self._delegate_line(elapsed)
        # A stated purpose is what this row is for: the widget is the one place
        # that shows a job while it runs, when the command has not paid off yet.
        command = (
            f"{event.purpose} · {command_preview(event.command)}"
            if event.command and event.purpose
            else command_preview(event.command)
            if event.command
            else ""
        )
        detail = plain(event.detail, limit=None)
        if event.name in JOB_HANDLE_TOOLS:
            # The id matches the job's own row and notices; the command says
            # what it runs. Neither is enough alone.
            detail = " · ".join(part for part in (detail, command) if part)
        else:
            detail = command or detail
        icon = f"{WAIT_ICON} " if event.name == "wait_for_job" else ""
        state = plain(event.activity) if event.activity else ""
        clock = "" if elapsed is None else f"{elapsed:.1f}s"
        return " · ".join(
            part for part in (f"{icon}{label(event.name)}", state, clock, detail) if part
        )

    def _delegate_line(self, elapsed: float | None) -> str:
        """`✦ Worker · 5.5s · Thinking · <task>`: the agent is what tells delegates apart.

        A settled delegate's last phase is stale (nearly always "Responding"),
        so it says how it ended instead.
        """
        event = self.event
        if self.settled is not None:
            state = "Failed" if self.failed else "Done"
        else:
            state = plain(event.activity) or "Starting"
        name = event.agent[:1].upper() + event.agent[1:] or label(event.name)
        task = event.task or plain(event.detail, limit=None)
        clock = "" if elapsed is None else f"{elapsed:.1f}s"
        return " · ".join(part for part in (f"{AGENT_ICON} {name}", clock, state, task) if part)


@dataclass
class ToolHistory:
    """Calls still in flight, oldest first. A result removes its call.

    A delegate leaves the same way, taking its plan and its own calls with it:
    the status row holds its outcome briefly and scrollback keeps it.
    """

    calls: list[ToolCall] = field(default_factory=list)
    # The last call to leave, kept only so the status row can hold it.
    recent: ToolCall | None = None
    # Each running delegate's plan, keyed by its call id.
    plans: dict[str, list[dict]] = field(default_factory=dict)

    def record_plan(self, call_id: str, items: list[dict]) -> None:
        if any(c.event.call_id == call_id for c in self.calls):
            self.plans[call_id] = items

    def record(self, event: ToolStarted | ToolSummary) -> None:
        # Planning operations have their own panel. Settled calls leave the
        # live view regardless of whether they need a scrollback entry.
        if event.name in PLAN_TOOLS:
            return
        existing = next(
            (c for c in self.calls if event.call_id and c.event.call_id == event.call_id), None
        )
        if isinstance(event, ToolSummary):
            if existing is None:
                return
            existing.failed = event.failed
            existing.settled = monotonic()
            self.recent = existing
            # A sub-agent's calls leave the way the parent's do: scrollback
            # keeps them under their delegate. A finished delegate takes any
            # still listed, and its plan, with it.
            call_id = existing.event.call_id
            self.calls = [
                c for c in self.calls if c is not existing and c.event.parent_call_id != call_id
            ]
            self.plans.pop(call_id, None)
        elif existing is not None:
            # A restated start carries fresh progress, not a new invocation.
            existing.event = event
        else:
            self.calls.append(ToolCall(event))

    def clear(self) -> None:
        self.calls.clear()
        self.plans.clear()
        self.recent = None

    @property
    def animating(self) -> bool:
        """Every call on the panel is live, so any of them keeps its clock ticking."""
        return bool(self.calls)

    @property
    def active(self) -> ToolCall | None:
        """What the status row above the tasks reports.

        The newest running call, or else the one that just finished, for as
        long as its dwell lasts. Anything that starts meanwhile wins the row:
        holding a stale line over live work would be the worse lie.
        """
        running = next((c for c in reversed(self.calls) if c.settled is None), None)
        if running is not None:
            return running
        held = self.recent
        if held is not None and monotonic() - (held.settled or held.started) < STATUS_DWELL:
            return held
        self.recent = None
        return None

    @property
    def running(self) -> int:
        """This agent's calls still in flight, for the status row's `Running N tools`.

        A delegate counts once; the calls its sub-agent makes are its own.
        """
        return sum(c.settled is None and not c.event.parent_call_id for c in self.calls)

    @property
    def background(self) -> list[ToolCall]:
        """Everything the status row does not already show."""
        active = self.active
        return [c for c in self.calls if c is not active]

    def _delegates(self) -> list[ToolCall]:
        """Delegates that get a panel row.

        Those beside the status row, plus any with a plan even while it holds
        the status row: otherwise its tasks would vanish every time the
        sub-agent went back to the model and the delegate took the row back.
        """
        active = self.active
        return [
            c
            for c in self.calls
            if c.event.name == DELEGATE and (c is not active or self.plans.get(c.event.call_id))
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
        return _tree_rows(self._nodes(count, icon), "" if nested else None)

    def _nodes(self, count: int, icon: str) -> list[_PanelNode]:
        """Delegates first: a running sub-agent must stay addressable and visible.

        Beneath each goes its plan, then its own calls, nested under its active
        task the way the parent's calls sit under the parent's. Both are bounded
        so several delegates cannot crowd each other out.
        """
        if count <= 0:
            return []
        calls = self.background
        delegates = self._delegates()[:count]
        remaining = count - len(delegates)
        nodes = []
        for parent in delegates:
            node = _PanelNode(_call_row(parent))
            nodes.append(node)
            plan = self.plans.get(parent.event.call_id, [])
            steps, active = plan_window(plan, min(CHILD_PLAN_ROWS, len(plan), remaining))
            remaining -= len(steps)
            children = [c for c in calls if c.event.parent_call_id == parent.event.call_id]
            children = children[-min(2, remaining) :] if remaining else []
            remaining -= len(children)
            child_nodes = [_PanelNode(_call_row(c)) for c in children]
            for index in steps:
                step = _PanelNode(plan_row(plan[index], icon))
                node.children.append(step)
                if index == active:
                    step.children = child_nodes
            if active is None or active not in steps:
                node.children.extend(child_nodes)
        if remaining:
            other = [c for c in calls if not c.event.parent_call_id and c.event.name != DELEGATE]
            nodes.extend(_PanelNode(_call_row(c)) for c in other[:remaining])
        return nodes


def _call_row(call: ToolCall) -> tuple[str, str]:
    """A running call's row.

    A delegate's `✦` stands in for the status icon, and it has a colour of
    its own: with a task's icon in front, a sub-agent would read as one of
    the parent's tasks, and as a child of whichever task names it.
    """
    if call.event.name == DELEGATE:
        return "class:plan.agent", call.line()
    return "class:plan.active", f"⟳ {call.line()}"


def plan_window(items: list[dict], count: int) -> tuple[range, int | None]:
    """The `count` steps worth showing, centred on the active one, and its index."""
    active = next((i for i, item in enumerate(items) if item["status"] == "in_progress"), None)
    anchor = active if active is not None else 0
    start = min(max(0, anchor - count // 2), max(0, len(items) - count))
    return range(start, start + count), active


def plan_row(item: dict, active_icon: str) -> tuple[str, str]:
    status = item["status"]
    style = "class:plan.active" if status == "in_progress" else "class:plan"
    icon = active_icon if status == "in_progress" else PLAN_ICONS.get(status, "○")
    return style, f"{icon} {plain(item['content'], limit=None)}"


def task_panel_rows(
    items: list[dict],
    tools: ToolHistory,
    budget: int,
    active_icon: str,
    max_tasks: int = TASK_ROWS,
):
    """A bounded task viewport, with any concurrent tool work below the active task.

    The newest call lives on the status row instead, so this only shows work
    running alongside it: delegates (with their own plans) and other parallel
    calls. Keep at least one task visible, even on short panes, and never add
    headers or empty rows.
    """
    if budget <= 0:
        return []
    tool_count = min(TOOL_ROWS + tools.plan_rows(), tools.wanted(), max(0, budget - bool(items)))
    steps, active = plan_window(items, min(max_tasks, len(items), budget - tool_count))
    nodes = []
    tool_nodes = tools._nodes(tool_count, active_icon)
    for index in steps:
        node = _PanelNode(plan_row(items[index], active_icon))
        nodes.append(node)
        if index == active:
            node.children = tool_nodes
    if active is None or active not in steps:
        nodes.extend(tool_nodes)
    return _tree_rows(nodes)


def panel_fragments(lines: list[tuple[str, str]], width: int):
    """Clip by terminal cells before prompt_toolkit renders non-wrapping rows."""
    fragments = []
    for index, (style, line) in enumerate(lines):
        text = Text(plain(line, limit=None))
        text.truncate(max(1, width), overflow="ellipsis")
        fragments.append((style, ("\n" if index else "") + text.plain))
    return fragments
