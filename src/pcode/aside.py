"""Side questions asked beside a running turn; no terminal imports.

A side question ("btw") reuses the conversation's context but is never part of
it: it is not written to the session journal, does not appear in the
conversation tree, and cannot be steered or resent. It runs on the
conversation's own agent, with the same instructions and tool definitions, so
its requests reuse the provider cache the conversation has already paid for.
That is also why its framing travels in the question rather than the system
prompt: anything added ahead of the history would miss the cache.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4

from pcode.preferences import EFFORTS

# One side question is a question, not a second conversation: it gets a small
# request budget and a hard deadline so a confused run cannot spend a session's
# worth of tokens in the background where nobody is watching it.
ASIDE_REQUEST_LIMIT = 12
ASIDE_TIMEOUT_SECONDS = 300
# Side questions accumulate over a long session; keep the recent ones readable
# rather than growing the viewer without bound.
ASIDE_HISTORY = 20
# `/btw $a $b QUESTION` fans out one side question per model. Each one sends
# the whole conversation, uncached on any model but the conversation's own.
ASIDE_MODEL_LIMIT = 4
MODEL_MARK = "$"
# `+high` asks on the conversation's model at that effort; `$model+high` on
# another model. The levels are the ones /effort accepts.
EFFORT_MARK = "+"
ASIDE_MODEL_USAGE = (
    f"Usage: /btw [$PROVIDER:MODEL[+EFFORT] | +EFFORT ...] QUESTION (EFFORT: {'|'.join(EFFORTS)})"
)

ASIDE_FRAMING = (
    "[Side question] The user is asking a side question about the conversation so far. "
    "Answer only this question, briefly, preferring what the conversation already shows "
    "over fresh investigation. Your answer appears in a popup beside the conversation "
    "and is not added to it, so do not address the main agent, continue its task, "
    "or promise work. Tools work normally, but the plan and delegation tools are "
    "unavailable here."
)


def framed(question: str) -> str:
    """The user message a side question is sent as."""
    return f"{ASIDE_FRAMING}\n\nQuestion: {question}"


@dataclass(frozen=True)
class SideTarget:
    """Where one side question runs: a model and the effort to ask it at.

    An empty `model` is the conversation's own; an empty `effort` keeps the
    effort that model already gets.
    """

    model: str = ""
    effort: str = ""


def split_effort(word: str) -> tuple[str, str]:
    """Split `model+high` into the model and its effort.

    Model ids can hold `:` and `/`, so only the part after the last `+` is
    considered, and only a real level counts: anything else stays in the name.
    """
    name, mark, effort = word.rpartition(EFFORT_MARK)
    if mark and effort in EFFORTS:
        return name, effort
    return word, ""


def _target(word: str) -> SideTarget:
    if word.startswith(EFFORT_MARK):
        effort = word.removeprefix(EFFORT_MARK)
        if effort not in EFFORTS:
            raise ValueError(f"Unknown effort `{word}`. {ASIDE_MODEL_USAGE}")
        return SideTarget(effort=effort)
    model, effort = split_effort(word.removeprefix(MODEL_MARK))
    if not model:
        raise ValueError(f"A model name must follow `{MODEL_MARK}`. {ASIDE_MODEL_USAGE}")
    return SideTarget(model, effort)


def parse_models(argument: str) -> tuple[list[SideTarget], str]:
    """Split `$model[+effort] [+effort ...] question` into targets and the question.

    Only leading `$` and `+` words name targets, so either one inside the
    question is text. Repeated targets collapse to one, in the order first
    given; the same model at two efforts is two targets.
    """
    models: list[SideTarget] = []
    rest = argument.strip()
    while rest.startswith((MODEL_MARK, EFFORT_MARK)):
        word, *tail = rest.split(maxsplit=1)
        rest = tail[0] if tail else ""
        target = _target(word)
        if target not in models:
            models.append(target)
    if models and not rest:
        raise ValueError(f"No question after the model. {ASIDE_MODEL_USAGE}")
    if len(models) > ASIDE_MODEL_LIMIT:
        raise ValueError(
            f"At most {ASIDE_MODEL_LIMIT} models per side question; got {len(models)}."
        )
    return models, rest


def _leading_word(argument: str) -> str | None:
    """The word being typed, while every word so far is a `$` or `+` target."""
    if not argument or argument[-1].isspace():
        return None
    words = argument.split()
    if not all(word.startswith((MODEL_MARK, EFFORT_MARK)) for word in words):
        return None
    return words[-1]


def model_fragment(argument: str) -> str | None:
    """The partial model name being typed in `/btw` arguments, if any.

    Only a word in the leading run of `$` and `+` words completes as a model;
    once the question has started, a `$` is ordinary text. A word already
    carrying `+` is choosing an effort instead; see `effort_fragment`.
    """
    word = _leading_word(argument)
    if word is None or not word.startswith(MODEL_MARK) or EFFORT_MARK in word:
        return None
    return word.removeprefix(MODEL_MARK)


def effort_fragment(argument: str) -> str | None:
    """The partial effort being typed after `+` in a leading `/btw` word, if any."""
    word = _leading_word(argument)
    if word is None or EFFORT_MARK not in word:
        return None
    return word.rpartition(EFFORT_MARK)[2]


def model_labels(models: list[SideTarget]) -> dict[SideTarget, str]:
    """Short labels: the model without its provider, unless two would collide.

    An effort joins the label, so answers at different efforts stay apart; the
    conversation's own model shows the effort alone.
    """
    names = list(dict.fromkeys(target.model for target in models if target.model))
    short = {name: name.partition(":")[2] or name for name in names}
    counts: dict[str, int] = {}
    for label in short.values():
        counts[label] = counts.get(label, 0) + 1
    labels = {}
    for target in models:
        label = short.get(target.model, "")
        if label and counts[label] > 1:
            label = target.model
        labels[target] = " \u00b7 ".join(part for part in (label, target.effort) if part)
    return labels


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
    # The model named with `/btw $MODEL`, and its short display form. Empty when
    # the question was asked without one, on the conversation's own model.
    model: str = ""
    label: str = ""
    # The effort named with `+EFFORT`; empty when the model's own applies.
    effort: str = ""
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
        # Given the exception of a question that failed or timed out, so the
        # frames can be kept where the session keeps turn failures.
        self.on_failure: Callable[[Aside, BaseException], None] = lambda aside, error: None

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

    def start(
        self,
        question: str,
        work: Callable[[Aside], Awaitable[None]],
        *,
        model: str = "",
        label: str = "",
        effort: str = "",
    ) -> Aside:
        """Register a side question and run `work` for it in the background."""
        aside = Aside(question=question, model=model, label=label, effort=effort)
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
        except TimeoutError as error:
            aside.settle("timed out", error=f"No answer within {ASIDE_TIMEOUT_SECONDS}s.")
            self._failed(aside, error)
        except asyncio.CancelledError:
            aside.settle("cancelled")
            raise
        except Exception as error:
            aside.settle("failed", error=error_message(error))
            self._failed(aside, error)
        else:
            aside.settle("answered")
        finally:
            self._tasks.pop(aside.id, None)
            self.on_settle(aside)

    def _failed(self, aside: Aside, error: BaseException) -> None:
        # Diagnostics must never replace the failure being diagnosed.
        try:
            self.on_failure(aside, error)
        except Exception:
            pass
