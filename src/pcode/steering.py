"""Deliver pending steering between model requests; never skip tools."""

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, UserPromptPart

# A question sent mid-turn was once answered only in thinking, with the reply
# deferred to a final message that then left it out; the user saw a promise and
# nothing else.
STEERING_REMINDER = (
    "[pcode] The user sent the message(s) above while you were working. If one asks "
    "something, answer it in visible text now, before resuming the task: thinking "
    "is not shown to the user, and an answer deferred to the end tends to get lost."
)


def run_request(ctx, request_context) -> ModelRequest:
    """The step's request as the run's history holds it, for parts that must persist.

    The request being sent can end in a message only it carries (a capability
    such as Harness's `WarnNearLimits` appends one), and a before-hook's request is not written
    back to history, so a part added to that message would be sent once and lost.
    The history's own request is in the request list too, so it is still sent.
    """
    last = ctx.messages[-1] if ctx.messages else None
    return last if isinstance(last, ModelRequest) else request_context.messages[-1]


class Steering(AbstractCapability):
    def __init__(
        self,
        take_messages,
        has_messages=lambda: False,
        prepare=None,
        reminder=None,
        commit_messages=None,
    ):
        self.take_messages = take_messages
        # A peek that consumes nothing, for waits that should hand back early.
        self.has_messages = has_messages
        # Turns a message's text into what the model is sent (the text and the
        # images it names); awaited, since that reads files.
        self.prepare = prepare
        # Sent after a delivery's messages, never on its own and never journaled.
        self.reminder = reminder
        # Optional deferred commit hook: when provided, `take_deferred` takes
        # messages without displaying/journaling them yet, returning a callback
        # that commits them once the model actually reaches the step boundary.
        self.commit_messages = commit_messages

    async def take(self, *, remind: bool = True) -> list:
        """The pending messages, consumed, each as the model is to be sent it.

        `remind=False` leaves out pcode's reminder, for an `antigravity:` turn
        the message steers natively (the reminder describes pcode's own loop).
        """
        messages = self.take_messages()
        if self.prepare is not None:
            messages = [await self.prepare(message) for message in messages]
        if messages and self.reminder and remind:
            messages = [*messages, self.reminder]
        return messages

    async def take_deferred(self, *, remind: bool = False) -> tuple[list, object]:
        """Consume pending messages now, deferring display/journal commit when supported.

        Returns `(prepared_messages, commit)` where `commit()` is idempotent and
        commits the taken messages to the view and journal.
        """
        if self.commit_messages is None:
            return await self.take(remind=remind), _noop
        raw = self.take_messages(defer_commit=True)
        committed = False

        def commit() -> None:
            nonlocal committed
            if committed or not raw:
                return
            committed = True
            self.commit_messages(raw)

        messages = [str(m) for m in raw]
        if self.prepare is not None:
            messages = [await self.prepare(message) for message in messages]
        if messages and self.reminder and remind:
            messages = [*messages, self.reminder]
        return messages, commit

    async def before_model_request(self, ctx, request_context):
        # Append to the framework's request so tool results remain ahead of the
        # new user input and the input is included in persisted message history.
        for content in await self.take():
            run_request(ctx, request_context).parts.append(UserPromptPart(content))
        return request_context


def _noop() -> None:
    pass


def _steering(ctx):
    """This run's own, never a parent's or a concurrent side question's input."""
    return [c for c in ctx.capabilities.values() if isinstance(c, Steering)]


def steering_pending(ctx) -> bool:
    return any(capability.has_messages() for capability in _steering(ctx))


async def take_user_steering(ctx) -> list:
    """Consume this run's steering as the user wrote it: one item per message, no reminder."""
    return [
        message for capability in _steering(ctx) for message in await capability.take(remind=False)
    ]


async def take_user_steering_deferred(ctx) -> tuple[list, object]:
    """Consume this run's steering as the user wrote it, deferring transcript commit."""
    messages = []
    commits = []
    for capability in _steering(ctx):
        taken, commit = await capability.take_deferred(remind=False)
        messages.extend(taken)
        if commit is not _noop:
            commits.append(commit)

    def commit_all() -> None:
        for commit in commits:
            commit()

    return messages, commit_all


async def take_steering(ctx) -> list:
    """Consume this run's steering now, for a hook with no model request to append to.

    One flat list of user content, as `RunContext.enqueue` takes it: a message
    that brought images is its text followed by them.
    """
    return [
        item
        for capability in _steering(ctx)
        for message in await capability.take()
        for item in ([message] if isinstance(message, str) else message)
    ]
