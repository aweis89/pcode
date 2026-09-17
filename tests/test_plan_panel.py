import asyncio
import json
from io import StringIO

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness.planning import Planning
from rich.console import Console

from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.runtime import PlanUpdated, ToolSummary
from pcode.sessions import SavedSession
from pcode.ui import TerminalOutput, create_prompt


def test_authoritative_plan_snapshots_cover_granular_tools_and_failures():
    calls = [
        ("write_plan", {"items": [{"id": "first", "content": "Inspect", "status": "in_progress"}]}),
        ("add_task", {"content": "Test"}),
        ("update_task_status", {"task_id": "first", "status": "completed"}),
        ("update_task_statuses", {"updates": [{"task_id": "first", "status": "pending"}]}),
        ("read_plan", {}),
        ("update_task_statuses", {"updates": [{"task_id": "missing", "status": "completed"}]}),
        ("remove_task", {"task_id": "first"}),
        ("write_plan", {"items": []}),
    ]
    requests = 0

    async def model(messages, info):
        nonlocal requests
        index = requests
        requests += 1
        if index < len(calls):
            name, args = calls[index]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}
        else:
            yield "Done"

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model), capabilities=[Planning()]))

    async def run():
        events = [event async for event in runtime.stream("Work")]
        snapshots = [event.items for event in events if isinstance(event, PlanUpdated)]
        assert [len(items) for items in snapshots] == [1, 2, 2, 2, 1, 0]
        assert snapshots[0][0]["status"] == "in_progress"
        assert snapshots[2][0]["status"] == "completed"
        assert snapshots[3][0]["status"] == "pending"
        # Harness-generated IDs, not IDs guessed by the UI from call arguments.
        assert snapshots[1][1]["id"]
        assert snapshots[4][0]["id"] == snapshots[1][1]["id"]
        errors = [e for e in events if isinstance(e, ToolSummary) and e.failed]
        assert len(errors) == 1
        assert errors[0].name == "update_task_statuses"

    asyncio.run(run())


def test_plan_survives_turns_resume_and_reset(tmp_path):
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {
                0: DeltaToolCall(
                    name="write_plan",
                    json_args=json.dumps(
                        {
                            "items": [
                                {
                                    "id": "step",
                                    "content": "Persistent task",
                                    "status": "in_progress",
                                }
                            ],
                        }
                    ),
                )
            }
        elif requests == 3:
            yield {
                0: DeltaToolCall(
                    name="update_task_status",
                    json_args=json.dumps(
                        {
                            "task_id": "step",
                            "status": "completed",
                        }
                    ),
                )
            }
        else:
            yield "Done"

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[Planning()]),
        saved,
    )

    async def run():
        first = [e async for e in runtime.stream("Start")]
        assert any(isinstance(e, PlanUpdated) for e in first)
        second = [e async for e in runtime.stream("Finish")]
        assert (
            next(e for e in second if isinstance(e, PlanUpdated)).items[0]["status"] == "completed"
        )
        for _ in range(50):
            saved.append("Message", markdown="Later output")
        # Recover the latest plan independently of the bounded transcript replay.
        restored = AgentRuntime(
            Agent(FunctionModel(stream_function=model), capabilities=[Planning()]),
            saved,
        )
        await restored.restore()
        assert (await restored.plan_store.get_items())[0].status == "completed"
        app = PreviewApp(model="test:local", runtime=restored, console=Console(file=StringIO()))
        app.replay()
        assert app.activity.plan[0]["content"] == "Persistent task"
        app.new("")
        assert app.activity.plan == []
        assert await restored.plan_store.get_items() == []

    try:
        asyncio.run(run())
    finally:
        saved.close()


def test_app_routes_plan_to_panel_not_transcript_and_preserves_errors():
    items = [{"id": "one", "content": "A task", "status": "in_progress"}]

    class Runtime:
        session = None

        async def stream(self, prompt):
            yield PlanUpdated(items)
            yield ToolSummary("write_plan", "Plan updated")
            yield ToolSummary("update_task_status", "No changes applied", failed=True)

    async def run():
        stream = StringIO()
        app = PreviewApp(model="test:local", runtime=Runtime(), console=Console(file=stream))
        with create_pipe_input() as pipe:
            prompt = create_prompt(app.registry, input=pipe, output=DummyOutput())
            output = TerminalOutput(app.transcript.console, prompt.app)
            assert await app.run_live(output, "Start")
        assert app.activity.plan == items
        assert "Plan updated" not in stream.getvalue()
        assert "No changes applied" not in stream.getvalue()
        assert len(app.activity.tools.calls) == 1
        assert app.activity.tools.calls[0].event.failed
        assert app.activity.tools.calls[0].event.detail == "No changes applied"

    asyncio.run(run())


def test_created_plan_exposes_ids_for_atomic_status_updates(tmp_path):
    import re

    from pydantic_ai.messages import ToolReturnPart

    from pcode.agent import create_coder
    from pcode.planning import IdentifiedPlanning

    planning = next(c for c in create_coder(tmp_path).capabilities if isinstance(c, Planning))
    assert isinstance(planning, IdentifiedPlanning)
    assert "not task IDs" in planning.get_instructions()
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {
                0: DeltaToolCall(
                    name="write_plan",
                    json_args=json.dumps(
                        {
                            "items": [
                                {"content": "Inspect", "status": "in_progress"},
                                {"content": "Test", "status": "pending"},
                            ]
                        }
                    ),
                )
            }
        elif requests == 2:
            result = next(
                part.content
                for message in reversed(messages)
                for part in message.parts
                if isinstance(part, ToolReturnPart) and part.tool_name == "write_plan"
            )
            ids = re.findall(r"task_id=([a-f0-9]{8})", result)
            assert len(ids) == 2
            yield {
                0: DeltaToolCall(
                    name="update_task_statuses",
                    json_args=json.dumps(
                        {
                            "updates": [
                                {"task_id": ids[0], "status": "completed"},
                                {"task_id": ids[1], "status": "in_progress"},
                            ],
                        }
                    ),
                )
            }
        else:
            yield "Done"

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model), capabilities=[planning]))

    async def run():
        events = [event async for event in runtime.stream("Work")]
        assert not any(isinstance(e, ToolSummary) and e.failed for e in events)
        items = await runtime.plan_store.get_items()
        assert [item.status for item in items] == ["completed", "in_progress"]

    asyncio.run(run())


@pytest.mark.parametrize("state", ["", "done", "failed", "cancelled"])
@pytest.mark.parametrize("busy", [False, True])
def test_unfinished_task_only_spins_during_live_turn(state, busy):
    from pcode.ui import Activity

    items = [{"id": "one", "content": "Unfinished task", "status": "in_progress"}]
    activity = Activity(plan=items, prompt_state=state, busy=busy)
    # Resumed, finished, failed, and cancelled turns stay static, even if input
    # is queued. Do not rewrite the persisted task's status to stop animation.
    first = activity.plan_rows(10, "⠋")
    assert first == activity.plan_rows(10, "⠙")
    assert first == [("class:plan.active", "  ○ Unfinished task")]
    assert items[0]["status"] == "in_progress"

    activity.prompt_state = "running"
    assert activity.plan_rows(10, "⠋") == [("class:plan.active", "  ⠋ Unfinished task")]
    assert activity.plan_rows(10, "⠙") == [("class:plan.active", "  ⠙ Unfinished task")]

    activity.prompt_state = "done"
    assert activity.plan_rows(10, "⠙") == first
