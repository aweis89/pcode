"""The parts of the session controller, tested without a terminal."""

import asyncio
from types import SimpleNamespace

from pcode.controller import PromptQueue


def panel():
    return SimpleNamespace(queued_prompts=[], queued_modes=[], queued=0)


def test_queue_keeps_the_panel_in_step():
    async def run():
        activity = panel()
        prompts = PromptQueue(activity)
        prompts.put("one", "queue")
        prompts.put("two", "steering")
        prompts.put("again", "resend", first=True)
        assert activity.queued_prompts == ["again", "one", "two"]
        assert activity.queued_modes == ["resend", "queue", "steering"]
        assert activity.queued == len(prompts) == 3
        item = await prompts.get()
        assert item == (0, "again", "resend") and prompts.current(item)
        prompts.taken()
        assert activity.queued_prompts == ["one", "two"] and activity.queued == 2

    asyncio.run(run())


def test_steering_is_taken_out_of_line_and_the_rest_keep_their_order():
    async def run():
        activity = panel()
        prompts = PromptQueue(activity)
        prompts.put("queued first", "queue")
        prompts.put("steer me", "steering")
        prompts.put("queued second", "queue")
        assert prompts.take_steering() == ["steer me"]
        assert activity.queued_prompts == ["queued first", "queued second"]
        assert activity.queued == 2
        assert [(await prompts.get())[1] for _ in range(2)] == ["queued first", "queued second"]

    asyncio.run(run())


def test_clear_starts_a_generation_that_makes_fetched_items_stale():
    async def run():
        activity = panel()
        prompts = PromptQueue(activity)
        prompts.put("fetched before the clear", "queue")
        item = await prompts.get()
        prompts.put("dropped", "queue")
        assert prompts.clear() == 2
        assert not prompts.current(item)
        assert activity.queued_prompts == [] and activity.queued == 0
        prompts.put("after", "steering")
        # Steering from before a clear is never delivered.
        assert prompts.take_steering() == ["after"]

    asyncio.run(run())
