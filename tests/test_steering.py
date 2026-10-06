"""Steering through the real runtime: delivered after the batch, never skipping it."""

import asyncio
import json

from pydantic_ai import Agent
from pydantic_ai.messages import RetryPromptPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from pcode.agent import create_coder
from pcode.live import AgentRuntime
from pcode.runtime import Message

STEER = "FYI, also mention the changelog."


def test_pending_steering_lets_the_whole_batch_run_then_arrives_once(tmp_path):
    """An FYI must not make the model redo work: every requested call still runs."""

    async def run():
        pending = []
        taken = []
        requests = 0

        def take():
            messages = pending[:]
            pending.clear()
            taken.append(messages)
            return messages

        async def model(messages, info):
            nonlocal requests
            requests += 1
            if requests == 1:
                # Arrives while the model is still generating its tool calls.
                pending.append(STEER)
                for index, name in enumerate(("one.txt", "two.txt")):
                    args = {"path": name, "content": name}
                    yield {index: DeltaToolCall(name="write_file", json_args=json.dumps(args))}
                return
            assert requests == 2
            assert taken == [[], [STEER]]
            parts = messages[-1].parts
            assert [type(part) for part in parts] == [
                ToolReturnPart,
                ToolReturnPart,
                UserPromptPart,
            ]
            assert all(part.content.startswith("Wrote ") for part in parts[:2])
            assert parts[-1].content == STEER
            yield "Done, with the changelog noted"

        runtime = AgentRuntime(
            Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
        )
        runtime.take_steering = take
        runtime.has_steering = lambda: bool(pending)
        events = [event async for event in runtime.stream("Write two files")]
        assert Message("Done, with the changelog noted") in events
        assert (tmp_path / "one.txt").read_text() == "one.txt"
        assert (tmp_path / "two.txt").read_text() == "two.txt"
        assert not pending and requests == 2
        history = [part for message in runtime.history for part in message.parts]
        assert not any(isinstance(part, RetryPromptPart) for part in history)

    asyncio.run(asyncio.wait_for(run(), timeout=10))
