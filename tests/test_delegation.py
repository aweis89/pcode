import asyncio
import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness.subagents import SubAgent, SubAgents

from pcode.agent import create_coder
from pcode.delegation import DelegationReporting, _parent, stream_child_activity
from pcode.live import AgentRuntime
from pcode.runtime import Message, TextDelta, ToolStarted, ToolSummary
from pcode.tool_display import delegation_detail, target
from pcode.tool_panel import ToolHistory, panel_fragments, task_panel_rows


def returns(messages):
    return [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


def delegate(index=0):
    return DeltaToolCall(
        name="delegate_task",
        json_args=json.dumps({"agent_name": "explorer", "task": f"Read sample.txt ({index})"}),
        tool_call_id=f"parent-{index}",
    )


def test_real_parallel_children_are_correlated_streamed_and_inspectable(tmp_path):
    (tmp_path / "sample.txt").write_text("workspace evidence")

    async def model(messages, info):
        names = {t.name for t in info.function_tools}
        if "delegate_task" in names:
            if returns(messages):
                yield "Parent answer"
            else:
                yield {0: delegate(0), 1: delegate(1)}
        else:
            assert "read_file" in names
            assert not {"edit_file", "write_file", "run_command"} & names
            if returns(messages):
                yield "PRIVATE CHILD ANSWER"
            else:
                yield "PRIVATE CHILD PROSE"
                yield {
                    0: DeltaToolCall(
                        name="read_file",
                        json_args='{"path":"sample.txt"}',
                        tool_call_id="same-child-id",
                    )
                }

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        events = [e async for e in runtime.stream("Explore in parallel")]
        assert _parent.get() is None
        assert [e.markdown for e in events if isinstance(e, Message)] == ["Parent answer"]
        assert "PRIVATE" not in "".join(e.text for e in events if isinstance(e, TextDelta))
        children = [e for e in events if isinstance(e, ToolStarted) and e.parent_call_id]
        assert {e.parent_call_id for e in children} == {"parent-0", "parent-1"}
        assert {e.call_id for e in children} == {"parent-0:same-child-id", "parent-1:same-child-id"}
        for child in children:
            assert child.detail == "sample.txt"
            finished = next(
                e for e in events if isinstance(e, ToolSummary) and e.call_id == child.call_id
            )
            assert finished.parent_call_id == child.parent_call_id
            assert "workspace evidence" in finished.result
            assert finished.run_id == child.run_id
        parents = [e for e in events if isinstance(e, ToolSummary) and e.name == "delegate_task"]
        assert len(parents) == 2
        assert all(e.outcome == "ok" and not e.failed and "Completed" in e.detail for e in parents)
        assert all(e.result == "PRIVATE CHILD ANSWER" for e in parents)
        assert {
            e.activity for e in events if isinstance(e, ToolStarted) and e.name == "delegate_task"
        } >= {"Waiting for model", "Responding", "Working"}
        assert len(runtime.inspections.calls) == 4  # Phase changes don't create new calls.
        assert all(c.state == "succeeded" for c in runtime.inspections.calls)

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["timeout", "budget", "failed", "contained"])
def test_real_soft_outcomes_are_not_reported_as_success(outcome):
    async def child_model(messages, info):
        if outcome == "timeout":
            await asyncio.sleep(60)
        elif outcome == "failed":
            raise UnexpectedModelBehavior("private error")
        else:
            raise RuntimeError("private crash")
        yield "unreachable"

    child = SubAgent(
        Agent(FunctionModel(stream_function=child_model), name="explorer"),
        timeout_seconds=0.01 if outcome == "timeout" else None,
        usage_limits=UsageLimits(request_limit=0) if outcome == "budget" else None,
        on_failure="Fallback observation" if outcome == "failed" else None,
        contain_errors=outcome == "contained",
    )
    calls = 0

    async def parent_model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {0: delegate()}
        else:
            yield "Recovered"

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=parent_model),
            capabilities=[
                SubAgents(
                    agents=[child], agent_folders=None, event_stream_handler=stream_child_activity
                ),
                DelegationReporting(),
            ],
        )
    )

    async def run():
        events = [e async for e in runtime.stream("Explore")]
        summary = next(e for e in events if isinstance(e, ToolSummary))
        assert summary.failed
        assert summary.outcome == outcome
        assert "private" not in summary.detail
        assert (
            summary.detail
            == delegation_detail(
                {"agent_name": "explorer", "task": "Read sample.txt (0)"}, outcome
            )[0]
        )
        assert _parent.get() is None

    asyncio.run(run())


def test_delegation_display_redacts_and_bounds_assignment():
    detail = target(
        "delegate_task",
        {"agent_name": "explorer\x1b", "task": "token=sk-" + "x" * 80 + "\n" + "long " * 100},
    )
    assert "\x1b" not in detail and "\n" not in detail
    assert len(detail) < 225
    assert "x" * 80 not in detail
    assert "agent unavailable" in target("delegate_task", {})


def test_active_delegations_are_pinned_and_children_share_the_row_budget():
    history = ToolHistory()
    history.record(ToolStarted("delegate_task", "explorer · investigate", "parent"))
    for i in range(20):
        history.record(ToolSummary("read_file", f"file-{i}", call_id=str(i)))
    history.record(ToolStarted("read_file", "child.py", "parent:child", parent_call_id="parent"))
    history.record(
        ToolSummary("read_file", "other.py", call_id="parent:done", parent_call_id="parent")
    )
    assert len(history.calls) == 10
    for budget in range(1, 10):
        rows = task_panel_rows([], history, budget, "⟳")
        assert len(rows) <= min(3, budget)
        assert "explorer" in rows[0][1]
        assert len(panel_fragments(rows, 20)) == len(rows)
    rows = history.rows(3)
    assert all(row[1].startswith("    ") for row in rows[1:])
    assert "child.py" in rows[1][1]
    # Finishing the plan must not hide a still-running child agent.
    assert any(
        "explorer" in text
        for _, text in task_panel_rows(
            [{"content": "done", "status": "completed"}], history, 4, "⟳"
        )
    )
    history.interrupt_running()
    assert not history._running
    assert not any(c.running for c in history.calls)


def test_parallel_parents_take_priority_over_child_chatter():
    history = ToolHistory()
    for i in range(4):
        history.record(ToolStarted("delegate_task", f"explorer-{i}", str(i)))
        history.record(ToolStarted("read_file", "file", f"{i}:child", parent_call_id=str(i)))
    rows = history.rows(3)
    assert len(rows) == 3
    assert all("Delegate" in text and "Read" not in text for _, text in rows)


def test_timeout_settles_inflight_child_tool_before_parent_resumes():
    stopped = False

    async def hold():
        nonlocal stopped
        try:
            await asyncio.sleep(60)
        finally:
            stopped = True

    async def child_model(messages, info):
        yield {0: DeltaToolCall(name="hold", json_args="{}", tool_call_id="held")}

    async def parent_model(messages, info):
        if returns(messages):
            yield "Recovered"
        else:
            yield {0: delegate()}

    child = Agent(FunctionModel(stream_function=child_model), name="explorer", tools=[hold])
    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=parent_model),
            capabilities=[
                SubAgents(
                    agents=[SubAgent(child, timeout_seconds=0.05)],
                    agent_folders=None,
                    event_stream_handler=stream_child_activity,
                ),
                DelegationReporting(),
            ],
        )
    )

    async def run():
        events = [e async for e in runtime.stream("Explore")]
        child_end = next(e for e in events if isinstance(e, ToolSummary) and e.parent_call_id)
        assert child_end.outcome == "interrupted" and child_end.failed
        parent_end = next(e for e in events if isinstance(e, ToolSummary) and not e.parent_call_id)
        assert parent_end.outcome == "timeout"
        assert events.index(child_end) < events.index(parent_end)
        assert stopped

    asyncio.run(run())


@pytest.mark.parametrize("crash", [False, True])
def test_cancel_or_propagated_crash_cleans_up_real_child_and_panel(crash):
    from io import StringIO
    from types import SimpleNamespace

    from prompt_toolkit.output import DummyOutput
    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.ui import CursorSafeOutput, TerminalOutput

    async def run():
        gate = asyncio.Event()
        stopped = asyncio.Event()

        async def hold():
            try:
                await gate.wait()
                raise RuntimeError("child crashed")
            finally:
                stopped.set()

        async def child_model(messages, info):
            yield {0: DeltaToolCall(name="hold", json_args="{}", tool_call_id="held")}

        async def parent_model(messages, info):
            yield {0: delegate()}

        child = Agent(FunctionModel(stream_function=child_model), name="explorer", tools=[hold])
        runtime = AgentRuntime(
            Agent(
                FunctionModel(stream_function=parent_model),
                capabilities=[
                    SubAgents(
                        agents=[SubAgent(child)],
                        agent_folders=None,
                        event_stream_handler=stream_child_activity,
                    ),
                    DelegationReporting(),
                ],
            )
        )
        app = PreviewApp(model="test:local", runtime=runtime, console=Console(file=StringIO()))
        output = TerminalOutput(
            app.transcript.console,
            SimpleNamespace(
                output=CursorSafeOutput(DummyOutput()),
                invalidate=lambda: None,
            ),
        )
        task = asyncio.create_task(app.run_live(output, "Explore"))
        try:
            async with asyncio.timeout(3):
                while not any(c.event.parent_call_id for c in app.activity.tools.calls):
                    await asyncio.sleep(0.001)
            if crash:
                gate.set()
            else:
                task.cancel()
            assert not await task
            assert stopped.is_set()
            assert not app.activity.tools._running
            assert all(c.interrupted for c in app.activity.tools.calls)
        finally:
            if not task.done():
                task.cancel()
                await task

    asyncio.run(run())


def test_replay_keeps_child_identity_and_interrupted_delegation(tmp_path):
    from io import StringIO
    from types import SimpleNamespace

    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.sessions import SavedSession

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        saved.append("turn_started", prompt="Explore")
        saved.event(
            ToolStarted("delegate_task", "explorer · Explore", "parent", activity="Thinking")
        )
        saved.event(ToolStarted("read_file", "file.py", "child", parent_call_id="parent"))
        saved.append("turn_cancelled")
        app = PreviewApp(
            model="test:local",
            runtime=SimpleNamespace(session=saved),
            console=Console(file=StringIO()),
        )
        app.replay()
        parent, child = app.activity.tools.calls
        assert parent.event.activity == "Thinking"
        assert child.event.parent_call_id == "parent"
        assert parent.interrupted and child.interrupted
        assert not app.activity.tools._running
    finally:
        saved.close()


def test_rejected_delegation_has_no_false_success():
    async def child_model(messages, info):
        raise AssertionError("A refused child must not run")
        yield "unreachable"

    async def parent_model(messages, info):
        if returns(messages):
            yield "Use existing evidence"
        else:
            yield {0: delegate()}

    child = Agent(FunctionModel(stream_function=child_model), name="explorer")
    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=parent_model),
            capabilities=[
                SubAgents(
                    agents=[SubAgent(child, max_calls=0)],
                    agent_folders=None,
                    event_stream_handler=stream_child_activity,
                ),
                DelegationReporting(),
            ],
        )
    )

    async def run():
        events = [e async for e in runtime.stream("Explore")]
        summary = next(e for e in events if isinstance(e, ToolSummary))
        assert summary.failed and summary.outcome == "not_started"
        assert "Not started" in summary.detail

    asyncio.run(run())


@pytest.mark.parametrize("interrupt", [False, True])
def test_evicted_delegate_remains_visible_when_it_settles(interrupt):
    history = ToolHistory()
    history.record(ToolStarted("delegate_task", "explorer · investigate", "parent"))
    for i in range(20):
        history.record(ToolSummary("read_file", f"file-{i}", call_id=str(i)))
    if interrupt:
        history.interrupt_running()
        assert "interrupted" in history.rows(1)[0][1]
    else:
        history.record(ToolSummary("delegate_task", "explorer → Completed", call_id="parent"))
        assert "Completed" in history.rows(1)[0][1]
    assert len(history.calls) == 10
    assert not history._running
