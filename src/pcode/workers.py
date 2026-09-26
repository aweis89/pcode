"""Each delegated worker's own stream, kept for the read-only viewer; no terminal imports.

The conversation's transcript shows a worker only as its delegate row and the
calls beneath it. This keeps what the worker itself said and did, in order, so
`/workers` can show it while it runs and after it settles. Memory only: nothing
here is journaled, so a resumed session starts with no workers.
"""

from dataclasses import dataclass, field
from time import monotonic

from pcode.runtime import ChildPlan, ChildText, ToolStarted, ToolSummary
from pcode.tool_display import PLAN_TOOLS

DELEGATE = "delegate_task"
# Workers accumulate over a long session; keep the recent ones.
WORKER_HISTORY = 20


@dataclass
class WorkerEntry:
    """One step of a worker's stream: a run of prose or reasoning, or one tool call."""

    kind: str  # "text", "thinking" or "tool"
    text: str = ""
    tool: ToolStarted | ToolSummary | None = None


@dataclass
class Worker:
    call_id: str
    agent: str
    task: str
    started: float = field(default_factory=monotonic)
    settled: float | None = None
    failed: bool = False
    # Why it stopped when that was not a normal finish, e.g. "Interrupted".
    ending: str = ""
    activity: str = ""
    plan: list[dict] = field(default_factory=list)
    entries: list[WorkerEntry] = field(default_factory=list)
    # Bumped on every change, so a viewer can skip unchanged repaints.
    version: int = 0

    @property
    def running(self) -> bool:
        return self.settled is None

    def elapsed(self) -> float:
        return (self.settled if self.settled is not None else monotonic()) - self.started

    def state(self) -> str:
        if self.running:
            return self.activity or "Starting"
        return self.ending or ("Failed" if self.failed else "Done")

    def add_text(self, event: ChildText) -> None:
        kind = "thinking" if event.thinking else "text"
        last = self.entries[-1] if self.entries else None
        if event.start or last is None or last.kind != kind:
            self.entries.append(WorkerEntry(kind, event.text))
        else:
            last.text += event.text

    def add_tool(self, event: ToolStarted | ToolSummary) -> None:
        """A start adds a row; its restatement or result replaces that row."""
        for entry in reversed(self.entries):
            if entry.tool is not None and entry.tool.call_id == event.call_id:
                entry.tool = event
                return
        self.entries.append(WorkerEntry("tool", tool=event))


@dataclass
class Workers:
    items: list[Worker] = field(default_factory=list)

    def get(self, call_id: str) -> Worker | None:
        return next((w for w in self.items if w.call_id == call_id), None)

    def record(self, event) -> None:
        """Fold one runtime event into the worker it belongs to, if any."""
        if isinstance(event, ToolStarted | ToolSummary) and event.name == DELEGATE:
            self._delegate(event)
            return
        call_id = (
            event.call_id
            if isinstance(event, ChildText | ChildPlan)
            else event.parent_call_id
            if isinstance(event, ToolStarted | ToolSummary)
            else ""
        )
        worker = self.get(call_id) if call_id else None
        if worker is None:
            return
        if isinstance(event, ChildText):
            worker.add_text(event)
        elif isinstance(event, ChildPlan):
            worker.plan = event.items
        elif event.name not in PLAN_TOOLS:
            # The plan has its own section; its tool calls would only repeat it.
            worker.add_tool(event)
        worker.version += 1

    def _delegate(self, event: ToolStarted | ToolSummary) -> None:
        worker = self.get(event.call_id)
        if isinstance(event, ToolStarted):
            if worker is None:
                worker = Worker(event.call_id, event.agent or "worker", event.task)
                self.items.append(worker)
                del self.items[:-WORKER_HISTORY]
            # A restated start carries the worker's current phase.
            worker.activity = event.activity
        elif worker is not None and worker.running:
            worker.settled = monotonic()
            worker.failed = event.failed
        else:
            return
        worker.version += 1

    def end_turn(self) -> None:
        """A turn that ends under a running worker stopped it, e.g. by cancellation."""
        for worker in self.items:
            if worker.running:
                worker.settled = monotonic()
                worker.failed = True
                worker.ending = "Interrupted"
                worker.version += 1

    def running(self) -> int:
        return sum(worker.running for worker in self.items)

    def latest(self) -> Worker | None:
        """The newest running worker, else the newest one."""
        running = [worker for worker in self.items if worker.running]
        return (running or self.items or [None])[-1]
