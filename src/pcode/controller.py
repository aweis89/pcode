"""What runs a conversation, lifted out of the terminal UI a piece at a time.

See docs/background-sessions-plan.md. The end state is a `SessionController`
that runs in the session host while the terminal only renders; until then its
parts live here and `PreviewApp` drives them.
"""

import asyncio

# One queued message: the queue generation it was sent in, its text, and how
# it is sent ("steering", "queue", "interrupt", "shell", "resend", "wake", and
# the "follow" modes of a turn a host started on its own).
Item = tuple[int, str, str]


class PromptQueue:
    """Messages waiting for the model, in order, mirrored into the live panel.

    The panel lists them from `activity.queued_prompts` and `queued_modes`, so
    every change goes through here and the two cannot drift. `generation`
    moves on at every `clear`: an item fetched before a clear, while a command
    or popup held the queue up, is recognised as stale and skipped. Commands
    are stamped with it too, for the same reason.
    """

    def __init__(self, activity) -> None:
        self.activity = activity
        self.generation = 0
        self._items: asyncio.Queue[Item] = asyncio.Queue()

    def __len__(self) -> int:
        return len(self.activity.queued_prompts)

    def put(self, text: str, mode: str, *, first: bool = False) -> None:
        """Queue `text`; `first` puts it ahead of everything already waiting."""
        if first:
            waiting = self._drain()
            self._items.put_nowait((self.generation, text, mode))
            for item in waiting:
                self._items.put_nowait(item)
            self.activity.queued_prompts.insert(0, text)
            self.activity.queued_modes.insert(0, mode)
        else:
            self._items.put_nowait((self.generation, text, mode))
            self.activity.queued_prompts.append(text)
            self.activity.queued_modes.append(mode)
        self._sync()

    async def get(self) -> Item:
        """The next item, stale or not; check `current` before acting on it."""
        return await self._items.get()

    def current(self, item: Item) -> bool:
        return item[0] == self.generation

    def taken(self) -> None:
        """The item just fetched is being acted on: drop it from the panel."""
        self.activity.queued_prompts.pop(0)
        self.activity.queued_modes.pop(0)
        self._sync()

    def clear(self) -> int:
        """Drop everything waiting and start a new generation. Returns how many were dropped."""
        self.generation += 1
        count = len(self.activity.queued_prompts)
        self._drain()
        self.activity.queued_prompts.clear()
        self.activity.queued_modes.clear()
        self._sync()
        return count

    def take_steering(self) -> list[str]:
        """Remove and return the current steering messages; everything else keeps its place."""
        messages = []
        for item in self._drain():
            generation, text, mode = item
            if generation == self.generation and mode == "steering":
                messages.append(text)
                index = next(
                    i
                    for i, queued in enumerate(
                        zip(self.activity.queued_prompts, self.activity.queued_modes)
                    )
                    if queued == (text, mode)
                )
                self.activity.queued_prompts.pop(index)
                self.activity.queued_modes.pop(index)
            else:
                self._items.put_nowait(item)
        self._sync()
        return messages

    def _drain(self) -> list[Item]:
        items = []
        while not self._items.empty():
            items.append(self._items.get_nowait())
        return items

    def _sync(self) -> None:
        self.activity.queued = len(self.activity.queued_prompts)
