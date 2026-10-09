import asyncio
import json

from pydantic_ai import Agent, CallToolsNode, RunContext
from pydantic_ai._run_context import dispatch_event_stream
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness.background_tools import BackgroundTools
from pydantic_ai_harness.background_tools import _capability as upstream
from pydantic_ai_harness.subagents import SubAgent, SubAgents

from pcode.background_delegation import BackgroundDelegation
from pcode.delegation import DelegationReporting, stream_child_activity
from pcode.live import AgentRuntime
from pcode.runtime import Message, RunStatus, ToolSummary


def delegate():
    args = {"agent_name": "explorer", "task": "Read the logs"}
    return DeltaToolCall(name="delegate_task", json_args=json.dumps(args), tool_call_id="call-1")


def last_request_text(messages) -> str:
    request = next(m for m in reversed(messages) if isinstance(m, ModelRequest))
    return "\n".join(
        str(part.content)
        for part in request.parts
        if isinstance(part, UserPromptPart | ToolReturnPart)
    )


class Harness:
    """A parent delegating to a child held open by `gate`, with steerable input."""

    def __init__(self):
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        # Holds the child's second call, once `gate` has released its first.
        self.second_gate = asyncio.Event()
        self.second_gate.set()
        self.stopped = asyncio.Event()
        self.steering: list[str] = []
        self.requests: list[str] = []

        async def hold() -> str:
            self.started.set()
            try:
                await self.gate.wait()
            finally:
                self.stopped.set()
            return "child evidence"

        async def hold_again() -> str:
            await self.second_gate.wait()
            return "more evidence"

        async def child_model(messages, info):
            done = {p.tool_name for m in messages for p in m.parts if isinstance(p, ToolReturnPart)}
            if "hold_again" in done:
                yield "CHILD REPORT"
            elif "hold" in done:
                yield {0: DeltaToolCall(name="hold_again", json_args="{}", tool_call_id="again")}
            else:
                yield {0: DeltaToolCall(name="hold", json_args="{}", tool_call_id="held")}

        async def parent_model(messages, info):
            text = last_request_text(messages)
            self.requests.append(text)
            if "CHILD REPORT" in text:
                yield "Final answer"
            elif "Second?" in text:
                yield "Second answer"
            elif "Status?" in text:
                yield "Still working on it"
            else:
                yield {0: delegate()}

        child = Agent(
            FunctionModel(stream_function=child_model), name="explorer", tools=[hold, hold_again]
        )
        self.runtime = AgentRuntime(
            Agent(
                FunctionModel(stream_function=parent_model),
                capabilities=[
                    SubAgents(
                        agents=[SubAgent(child)],
                        agent_folders=None,
                        event_stream_handler=stream_child_activity,
                    ),
                    DelegationReporting(),
                    BackgroundDelegation(),
                ],
            )
        )
        self.runtime.has_steering = lambda: bool(self.steering)
        self.runtime.take_steering = self.take

    def take(self) -> list[str]:
        taken, self.steering[:] = list(self.steering), []
        return taken


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def test_steering_reaches_the_parent_while_its_delegate_keeps_working():
    async def run():
        h = Harness()
        events = []

        async def consume():
            async for event in h.runtime.stream("Explore"):
                events.append(event)

        task = asyncio.create_task(consume())
        await asyncio.wait_for(h.started.wait(), 5)
        h.steering.append("Status?")
        # The parent answers while the child is still held open.
        await wait_for(lambda: any(isinstance(e, Message) for e in events))
        assert not h.gate.is_set() and not h.stopped.is_set()
        assert "moved to the background" in h.requests[1] and "Status?" in h.requests[1]
        delegate_ends = [e for e in events if isinstance(e, ToolSummary) and not e.parent_call_id]
        assert delegate_ends == []

        # The model ended its response; the run waits for the child, and more
        # steering still gets an answer without waiting for it.
        await wait_for(
            lambda: any(isinstance(e, RunStatus) and "sub-agent" in e.text for e in events)
        )
        h.steering.append("Second?")
        await wait_for(lambda: len(h.requests) == 3)
        assert "Second?" in h.requests[2]
        assert not h.stopped.is_set()

        # While the run waits, the child's progress still reaches the screen.
        h.second_gate.clear()
        h.gate.set()
        await wait_for(
            lambda: any(isinstance(e, ToolSummary) and e.call_id == "call-1:held" for e in events)
        )
        assert len(h.requests) == 3 and not task.done()
        h.second_gate.set()
        await asyncio.wait_for(task, 5)
        assert "Background tool 'delegate_task' (task call-1) completed" in h.requests[3]
        assert [e.markdown for e in events if isinstance(e, Message)] == [
            "Still working on it",
            "Second answer",
            "Final answer",
        ]
        (end,) = [e for e in events if isinstance(e, ToolSummary) and e.call_id == "call-1"]
        assert end.outcome == "ok" and not end.failed and "Completed" in end.detail
        assert "CHILD REPORT" in end.result

    asyncio.run(run())


def test_without_steering_a_delegation_returns_in_the_foreground():
    async def run():
        h = Harness()
        task = asyncio.create_task(_collect(h.runtime.stream("Explore")))
        await asyncio.wait_for(h.started.wait(), 5)
        h.gate.set()
        events = await asyncio.wait_for(task, 5)
        assert len(h.requests) == 2
        assert "CHILD REPORT" in h.requests[1]
        assert "Background tool" not in h.requests[1]
        (end,) = [e for e in events if isinstance(e, ToolSummary) and e.call_id == "call-1"]
        assert end.outcome == "ok"

    asyncio.run(run())


def test_cancelling_the_turn_cancels_a_detached_child():
    async def run():
        h = Harness()
        events = []

        async def consume():
            async for event in h.runtime.stream("Explore"):
                events.append(event)

        task = asyncio.create_task(consume())
        await asyncio.wait_for(h.started.wait(), 5)
        h.steering.append("Status?")
        await wait_for(lambda: any(isinstance(e, Message) for e in events))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert h.stopped.is_set()
        assert len(h.requests) == 2

    asyncio.run(run())


async def _collect(stream):
    return [event async for event in stream]


def test_upstream_internals_this_relies_on():
    """`BackgroundDelegation` reaches into these; a Harness upgrade must fail here first."""
    assert {"_task_group", "_live", "_send", "_outcomes"} <= {
        f for f in BackgroundTools.__dataclass_fields__
    }
    assert callable(BackgroundTools._arrived)
    assert callable(BackgroundTools._background_mode)
    for name in ("_deliver", "_format_background_error", "_format_background_result"):
        assert callable(getattr(upstream, name))
    # The end-of-run wait reads which node a stream belongs to and drains its buffer.
    assert "_next_node" in CallToolsNode.__dataclass_fields__
    assert "_event_stream_buffer" in RunContext.__dataclass_fields__
    assert callable(dispatch_event_stream)
