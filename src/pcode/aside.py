"""Side questions asked beside a running turn; no terminal imports.

A side question ("btw") reuses the conversation's context but is never part of
it: it is not written to the session journal, does not appear in the
conversation tree, and cannot be steered or resent. What it shares with the
conversation is the message prefix it was asked against and the session's token
totals, both of which are read-only concerns.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4

# One side question is a question, not a second conversation: it gets a small
# request budget and a hard deadline so a confused run cannot spend a session's
# worth of tokens in the background where nobody is watching it.
ASIDE_REQUEST_LIMIT = 12
ASIDE_TIMEOUT_SECONDS = 300
# Side questions accumulate over a long session; keep the recent ones readable
# rather than growing the viewer without bound.
ASIDE_HISTORY = 20


def settled_context(messages: list) -> list:
    """The longest prefix of `messages` whose tool calls all have results.

    The newest context available while a turn runs is the request in flight,
    which usually ends in tool calls that have not returned yet. Providers
    reject a history with an unanswered call, so a side question is asked
    against the last point where the conversation was balanced.

    The provider stack is imported here, not at module import: the terminal
    reaches its first frame without loading it. See `docs/dependencies.md`.
    """
    from pydantic_ai.messages import (
        NativeToolCallPart,
        NativeToolReturnPart,
        RetryPromptPart,
        ToolCallPart,
        ToolReturnPart,
    )

    calls = (ToolCallPart, NativeToolCallPart)
    results = (ToolReturnPart, NativeToolReturnPart, RetryPromptPart)
    outstanding: set[str] = set()
    balanced = 0
    for index, message in enumerate(messages, start=1):
        for part in message.parts:
            if isinstance(part, calls):
                outstanding.add(part.tool_call_id)
            elif isinstance(part, results):
                outstanding.discard(part.tool_call_id)
        if not outstanding:
            balanced = index
    return list(messages[:balanced])


@dataclass
class Aside:
    """One side question and whatever of its answer has arrived."""

    question: str
    id: str = field(default_factory=lambda: uuid4().hex[:8])
    # running → answered / failed / cancelled / timed out.
    status: str = "running"
    answer: str = ""
    activity: str = "Waiting for model…"
    error: str = ""
    started: float = field(default_factory=monotonic)
    finished: float | None = None
    # Whether the answer has been opened in the viewer since it settled.
    read: bool = False

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def elapsed(self) -> float:
        return (self.finished if self.finished is not None else monotonic()) - self.started

    def settle(self, status: str, *, error: str = "") -> None:
        self.status = status
        self.error = error
        self.activity = ""
        self.finished = monotonic()

    def state(self) -> str:
        """Short status for the viewer list and footer, e.g. `answered 12s`."""
        return f"{self.status} {self.elapsed:.0f}s"


class Asides:
    """Every side question this session asked, oldest first."""

    def __init__(self) -> None:
        self.items: list[Aside] = []
        self._tasks: dict[str, asyncio.Task] = {}
        # Set by the terminal to repaint while an answer streams and to announce
        # one that settled. Off-terminal callers (tests, `--print`) need neither.
        self.on_update: Callable[[Aside], None] = lambda aside: None
        self.on_settle: Callable[[Aside], None] = lambda aside: None

    @property
    def running(self) -> int:
        return sum(aside.running for aside in self.items)

    @property
    def unread(self) -> int:
        return sum(not aside.running and not aside.read for aside in self.items)

    def latest(self) -> Aside | None:
        """The answer a bare `/btw` should open: newest unread, else newest."""
        unread = [aside for aside in self.items if not aside.running and not aside.read]
        return (unread or self.items or [None])[-1]

    def start(self, question: str, work: Callable[[Aside], Awaitable[None]]) -> Aside:
        """Register a side question and run `work` for it in the background."""
        aside = Aside(question=question)
        self.items.append(aside)
        # Trim settled records only: a running question owns a live task.
        while len(self.items) > ASIDE_HISTORY:
            stale = next((item for item in self.items if not item.running), None)
            if stale is None:
                break
            self.items.remove(stale)
        self._tasks[aside.id] = asyncio.create_task(self._run(aside, work))
        return aside

    def cancel(self) -> int:
        """Stop every running side question; returns how many were stopped."""
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        return len(tasks)

    async def close(self) -> None:
        self.cancel()
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def update(self, aside: Aside, *, answer: str, activity: str) -> None:
        aside.answer = answer
        aside.activity = activity
        self.on_update(aside)

    async def _run(self, aside: Aside, work: Callable[[Aside], Awaitable[None]]) -> None:
        from pcode.live import error_message

        try:
            async with asyncio.timeout(ASIDE_TIMEOUT_SECONDS):
                await work(aside)
        except TimeoutError:
            aside.settle("timed out", error=f"No answer within {ASIDE_TIMEOUT_SECONDS}s.")
        except asyncio.CancelledError:
            aside.settle("cancelled")
            raise
        except Exception as error:
            aside.settle("failed", error=error_message(error))
        else:
            aside.settle("answered")
        finally:
            self._tasks.pop(aside.id, None)
            self.on_settle(aside)
