import asyncio
import json

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaThinkingPart, DeltaToolCall, FunctionModel
from pydantic_ai_harness.subagents import SubAgent, SubAgents

from pcode.delegation import DelegationReporting, stream_child_activity
from pcode.live import AgentRuntime
from pcode.runtime import ChildPlan, ChildText, Message, TextDelta, ToolStarted, ToolSummary
from pcode.stream_display import present_stream_event
from pcode.ui import Activity
from pcode.worker_ui import WorkerBrowser, details
from pcode.workers import Workers


def started(call_id="d1", task="fix parser", **fields):
    return ToolStarted(
        "delegate_task", f"worker · {task}", call_id, agent="worker", task=task, **fields
    )


def test_worker_prose_reaches_the_viewer_but_never_the_transcript():
    async def child_model(messages, info):
        yield {0: DeltaThinkingPart(content="pondering")}
        yield "private worker "
        yield "prose"

    calls = 0

    async def parent_model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                0: DeltaToolCall(
                    name="delegate_task",
                    json_args=json.dumps({"agent_name": "explorer", "task": "Look around"}),
                    tool_call_id="parent-0",
                )
            }
        else:
            yield "Parent done"

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=parent_model),
            capabilities=[
                SubAgents(
                    agents=[
                        SubAgent(Agent(FunctionModel(stream_function=child_model), name="explorer"))
                    ],
                    agent_folders=None,
                    event_stream_handler=stream_child_activity,
                ),
                DelegationReporting(),
            ],
        )
    )

    async def run():
        return [event async for event in runtime.stream("Explore")]

    events = asyncio.run(run())
    child = [e for e in events if isinstance(e, ChildText)]
    assert {e.call_id for e in child} == {"parent-0"}
    assert "".join(e.text for e in child if not e.thinking) == "private worker prose"
    assert "".join(e.text for e in child if e.thinking) == "pondering"
    visible = [e for e in events if isinstance(e, (TextDelta, Message))]
    assert all("private" not in getattr(e, "text", getattr(e, "markdown", "")) for e in visible)


def test_workers_fold_their_stream_in_order_and_settle():
    workers = Workers()
    workers.record(started())
    workers.record(ChildText("d1", "thinking", thinking=True, start=True))
    workers.record(ChildText("d1", "Hello ", start=True))
    workers.record(ChildText("d1", "there"))
    workers.record(ToolStarted("shell", "make test", "d1:c1", parent_call_id="d1"))
    workers.record(ToolSummary("shell", "make test → ok", call_id="d1:c1", parent_call_id="d1"))
    workers.record(ChildPlan("d1", [{"content": "Patch", "status": "in_progress"}]))
    workers.record(ChildText("other", "stray"))  # Not a known worker: ignored.
    worker = workers.latest()
    assert [(e.kind, e.text) for e in worker.entries[:2]] == [
        ("thinking", "thinking"),
        ("text", "Hello there"),
    ]
    assert isinstance(worker.entries[2].tool, ToolSummary)
    assert len(worker.entries) == 3 and worker.plan[0]["content"] == "Patch"
    assert worker.running and workers.running() == 1
    workers.record(ToolSummary("delegate_task", "worker · fix parser → Completed", call_id="d1"))
    assert worker.state() == "Done" and workers.running() == 0

    workers.record(started("d2", "second"))
    workers.end_turn()
    assert workers.get("d2").state() == "Interrupted"


def test_stream_display_routes_child_text_to_workers_only():
    activity = Activity()
    output = type("Output", (), {"app": type("App", (), {"invalidate": lambda self: None})()})()
    present_stream_event(
        ChildText("d1", "hi", start=True),
        output=output,
        transcript=None,
        activity=activity,
        present=lambda events: None,
    )
    assert activity.workers.items == []  # No delegate recorded yet, so nothing to attach to.
    activity.workers.record(started())
    present_stream_event(
        ChildText("d1", "hi", start=True),
        output=output,
        transcript=None,
        activity=activity,
        present=lambda events: None,
    )
    assert activity.workers.latest().entries[0].text == "hi"


def test_viewer_shows_plan_prose_and_calls_and_hides_thinking_by_default():
    workers = Workers()
    workers.record(started())
    workers.record(ChildPlan("d1", [{"content": "Patch parser", "status": "in_progress"}]))
    workers.record(ChildText("d1", "secret reasoning", thinking=True, start=True))
    workers.record(ChildText("d1", "Found **it**", start=True))
    workers.record(ToolStarted("read_file", "src/parser.py", "d1:c1", parent_call_id="d1"))
    worker = workers.latest()
    rendered = " ".join(
        str(getattr(b, "markup", b))
        for b in details(worker, code_theme="ansi_dark", show_thinking=False)
    )
    assert "Tasks 0/1" in rendered and "Patch parser" in rendered
    assert "Found **it**" in rendered and "src/parser.py" in rendered
    assert "secret reasoning" not in rendered
    shown = " ".join(str(b) for b in details(worker, code_theme="ansi_dark", show_thinking=True))
    assert "secret reasoning" in shown

    with create_pipe_input() as pipe:
        browser = WorkerBrowser(workers, input=pipe, output=DummyOutput())
        assert browser.selected == "d1"
        assert "Worker" in browser.list.text and "fix parser" in browser.list.text
