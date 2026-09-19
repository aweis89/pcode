import asyncio
import json
from io import StringIO

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness.planning import PlanItem, Planning
from rich.console import Console

from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.plan_preview import project_plan
from pcode.runtime import PlanPreview, PlanUpdated
from pcode.sessions import SavedSession
from pcode.ui import Activity, TerminalOutput, create_prompt


def test_rows_reach_pane_during_partial_arguments_without_persisting(tmp_path):
    async def run():
        first_seen, second_seen = asyncio.Event(), asyncio.Event()
        finish_model = asyncio.Event()
        requests = 0

        async def model(messages, info):
            nonlocal requests
            requests += 1
            if requests > 1:
                yield "Done"
                return
            yield {
                0: DeltaToolCall(
                    name="write_plan",
                    json_args='{"items":[{"content":"Inspect 世界", "status":"in_progress"},',
                )
            }
            await first_seen.wait()
            yield {0: DeltaToolCall(json_args='{"content":"Test", "status":"pending"}]}')}
            await second_seen.wait()
            await finish_model.wait()

        saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
        runtime = AgentRuntime(
            Agent(FunctionModel(stream_function=model), capabilities=[Planning()]), saved
        )
        app = PreviewApp(model="test:local", runtime=runtime, console=Console(file=StringIO()))
        original = runtime.stream

        async def observed(prompt):
            async for event in original(prompt):
                yield event
                # run_live has consumed the event and updated the pane by this point.
                if isinstance(event, PlanPreview) and event.items:
                    assert app.activity.displayed_plan == event.items
                    assert app.activity.plan == []
                    assert await runtime.plan_store.get_items() == []
                    assert saved.latest_plan() == []
                    assert runtime.tree.nodes[runtime.tree.active].plan == []
                    count = len(event.items)
                    assert app.activity.panel_title() == f"Tasks 0/{count}"
                    assert "Inspect 世界" in str(app.activity.plan_rows(10, "*"))
                    (first_seen if count == 1 else second_seen).set()

        runtime.stream = observed
        try:
            with create_pipe_input() as pipe:
                prompt = create_prompt(app.registry, input=pipe, output=DummyOutput())
                output = TerminalOutput(app.transcript.console, prompt.app)
                task = asyncio.create_task(app.run_live(output, "Start"))
                try:
                    await asyncio.wait_for(second_seen.wait(), 5)
                    assert not task.done()
                    finish_model.set()
                    assert await asyncio.wait_for(task, 5)
                finally:
                    if not task.done():
                        task.cancel()
                        await task
            assert app.activity.plan_preview is None
            assert [item["content"] for item in app.activity.plan] == ["Inspect 世界", "Test"]
            assert all(not item["id"].startswith("preview:") for item in app.activity.plan)
            assert saved.latest_plan() == app.activity.plan
            assert not any(r["kind"] == "PlanPreview" for r in saved.records())
        finally:
            saved.close()

    asyncio.run(run())


@pytest.mark.parametrize("ending", ["cancel", "error", "rejected"])
def test_unconfirmed_preview_rolls_back_on_cancel_error_or_rejected_tool(ending):
    async def run():
        preview_seen, release = asyncio.Event(), asyncio.Event()
        requests = 0
        confirmed = PlanItem(id="old", content="Confirmed", status="pending")

        async def model(messages, info):
            nonlocal requests
            requests += 1
            if requests > 1:
                yield "Done"
                return
            # Duplicate IDs are individually displayable but Harness rejects the plan.
            yield {
                0: DeltaToolCall(
                    name="write_plan",
                    json_args=json.dumps(
                        {
                            "items": [
                                {
                                    "id": "duplicate",
                                    "content": "Provisional A",
                                    "status": "in_progress",
                                },
                                {
                                    "id": "duplicate",
                                    "content": "Provisional B",
                                    "status": "pending",
                                },
                            ]
                        }
                    ),
                )
            }
            await release.wait()
            if ending == "error":
                raise RuntimeError("stream failed")

        runtime = AgentRuntime(
            Agent(FunctionModel(stream_function=model), capabilities=[Planning()])
        )
        await runtime.plan_store.set_items([confirmed])
        printed = StringIO()
        app = PreviewApp(model="test:local", runtime=runtime, console=Console(file=printed))
        app.activity.plan = [confirmed.model_dump(mode="json")]
        original = runtime.stream

        async def observed(prompt):
            async for event in original(prompt):
                yield event
                if isinstance(event, PlanPreview) and event.items:
                    assert app.activity.displayed_plan[0]["content"] == "Provisional A"
                    preview_seen.set()

        runtime.stream = observed
        with create_pipe_input() as pipe:
            prompt = create_prompt(app.registry, input=pipe, output=DummyOutput())
            task = asyncio.create_task(
                app.run_live(TerminalOutput(app.transcript.console, prompt.app), "Start")
            )
            try:
                await asyncio.wait_for(preview_seen.wait(), 5)
                if ending == "cancel":
                    task.cancel()
                else:
                    release.set()
                assert await asyncio.wait_for(task, 5) == (ending == "rejected")
            finally:
                if not task.done():
                    task.cancel()
                    await task
        assert app.activity.plan_preview is None
        assert app.activity.displayed_plan == [confirmed.model_dump(mode="json")]
        assert await runtime.plan_store.get_items() == [confirmed]
        if ending == "rejected":
            assert "✗ Plan failed" in printed.getvalue()

    asyncio.run(run())


def test_settled_calls_reconcile_without_waiting_for_slow_sibling():
    async def run():
        finish_slow = asyncio.Event()
        requests = 0

        async def model(messages, info):
            nonlocal requests
            requests += 1
            if requests == 1:
                yield {
                    0: DeltaToolCall(name="add_task", json_args='{"content":"First"}'),
                    1: DeltaToolCall(name="slow", json_args="{}"),
                }
            elif requests == 2:
                yield {0: DeltaToolCall(name="add_task", json_args='{"content":"Second"}')}
            else:
                yield "Done"

        agent = Agent(FunctionModel(stream_function=model), capabilities=[Planning()])

        @agent.tool_plain
        async def slow() -> str:
            await asyncio.wait_for(finish_slow.wait(), 5)
            return "Done"

        runtime = AgentRuntime(agent)
        confirmed, previews = [], []
        async for event in runtime.stream("Work"):
            if isinstance(event, PlanUpdated):
                confirmed = event.items
                finish_slow.set()
            elif isinstance(event, PlanPreview):
                previews.append(event.items)
                if event.items is None and len(confirmed) == 1:
                    assert finish_slow.is_set()
        assert [item["content"] for item in confirmed] == ["First", "Second"]
        assert [len(items) for items in previews if items is not None] == [1, 2]
        assert previews[-1] is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "name,args,expected",
    [
        ("write_plan", '{"items":[', ["Old"]),
        ("write_plan", '{"items":[{"content":"not finished', ["Old"]),
        ("write_plan", '{"items":[]}', []),
        ("write_plan", '{"items":[{"content":42}]}', ["Old"]),
        ("write_plan", "not json", ["Old"]),
        ("add_task", '{"content":"New"', ["Old", "New"]),
        ("add_task", '{"content":"New", "status":"nonsense"}', ["Old"]),
        ("remove_task", '{"task_id":"old"}', []),
        ("remove_task", '{"task_id":"ol', ["Old"]),
    ],
)
def test_partial_projection_is_defensive(name, args, expected):
    original = [PlanItem(id="old", content="Old").model_dump(mode="json")]
    projected = project_plan(original, ToolCallPart(name, args, "call"))
    assert [item["content"] for item in projected] == expected
    assert original[0]["content"] == "Old"


@pytest.mark.parametrize("batched", [False, True])
def test_status_preview_does_not_mutate_confirmed_items(batched):
    original = [PlanItem(id="old", content="Old").model_dump(mode="json")]
    update = {"task_id": "old", "status": "completed"}
    part = ToolCallPart(
        "update_task_statuses" if batched else "update_task_status",
        {"updates": [update]} if batched else update,
        "call",
    )
    projected = project_plan(original, part)
    assert projected[0]["status"] == "completed"
    assert original[0]["status"] == "pending"
    activity = Activity(plan=original, plan_preview=projected)
    assert activity.panel_title() == "Tasks 1/1"
    activity.reset()
    assert activity.plan_preview is None
    assert activity.displayed_plan == []


@pytest.mark.parametrize("first_result", ["add_task", "other_tool"])
def test_concurrent_settlements_do_not_project_additions_twice(first_result):
    from pydantic_ai.messages import FunctionToolResultEvent, PartStartEvent, ToolReturnPart

    from pcode.plan_preview import StreamingPlanPreview

    preview = StreamingPlanPreview()
    for index, content in enumerate(["First", "Second"]):
        event = preview.update(
            PartStartEvent(
                index=index, part=ToolCallPart("add_task", {"content": content}, str(index))
            ),
            [],
        )
        assert len(event.items) == index + 1
    # Both tools can mutate the shared store before either result is consumed.
    confirmed = [
        PlanItem(id=str(i), content=content).model_dump(mode="json")
        for i, content in enumerate(["First", "Second"])
    ]
    event = preview.update(
        FunctionToolResultEvent(ToolReturnPart(first_result, "Done", "0")), confirmed
    )
    assert event == PlanPreview(None)


def test_rejected_call_does_not_discard_pending_sibling_preview():
    from pydantic_ai.messages import FunctionToolResultEvent, PartStartEvent, ToolReturnPart

    from pcode.plan_preview import StreamingPlanPreview

    preview = StreamingPlanPreview()
    for index, content in enumerate(["Rejected", "Still pending"]):
        preview.update(
            PartStartEvent(
                index=index, part=ToolCallPart("add_task", {"content": content}, str(index))
            ),
            [],
        )
    event = preview.update(FunctionToolResultEvent(ToolReturnPart("add_task", "Error", "0")), [])
    assert [item["content"] for item in event.items] == ["Still pending"]
