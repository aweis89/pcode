"""Context rewrites must survive restart/checkout without replaying tool effects."""

import asyncio
from copy import deepcopy

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage
from pydantic_ai_harness.compaction import ClearToolResults
from pydantic_ai_harness.planning import PlanItem
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, is_provider_valid

from pcode.agent import create_coder
from pcode.compaction import (
    MARKER,
    CompactionError,
    context_estimate,
    effective_window,
    summarize,
)
from pcode.context_usage import context_label
from pcode.live import AgentRuntime
from pcode.sessions import SavedSession

SUMMARY = "## Goal and constraints\nFix auth.\n## Verification\nTests failed; not yet fixed."


def history():
    return [
        ModelRequest(
            parts=[SystemPromptPart("Do not commit secrets."), UserPromptPart("Fix auth")]
        ),
        ModelResponse(parts=[ToolCallPart("read_file", {"path": "auth.py"}, "read-1")]),
        ModelRequest(parts=[ToolReturnPart("read_file", "important failure " * 6000, "read-1")]),
        ModelResponse(parts=[TextPart("Investigating " * 6000)]),
        ModelRequest(parts=[UserPromptPart("Preserve test failures.")]),
        ModelResponse(parts=[TextPart("Still working")], usage=RequestUsage(input_tokens=50000)),
    ]


def summary_model(calls, *, output=SUMMARY):
    async def stream(messages, info):
        calls.append((deepcopy(messages), info))
        yield output

    return FunctionModel(stream_function=stream)


def test_summarizer_is_tool_free_focused_incremental_and_pair_safe(monkeypatch):
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "100000")

    async def run():
        calls = []
        source = history()
        original = deepcopy(source)
        usage = RunUsage()
        result = await summarize(
            source, model=summary_model(calls), focus="Keep {literal} test failures", usage=usage
        )
        assert result.changed and result.after < result.before
        assert source == original
        assert is_provider_valid(result.messages)
        assert usage.requests == 1
        assert not calls[0][1].function_tools
        prompt = calls[0][0][-1].parts[0].content
        assert "Keep {literal} test failures" in prompt
        assert "important failure" in prompt
        assert "Do not commit secrets." in str(result.messages)
        assert "Still working" in str(result.messages)
        assert context_estimate(result.messages) == result.after
        assert "ctx: ~" in context_label("test:local", result.messages)
        # Recompaction anchors the previous summary instead of erasing it.
        second_source = result.messages + history()[1:]
        second = await summarize(second_source, model=summary_model(calls))
        assert second.changed
        assert "<previous-summary>" in calls[-1][0][-1].parts[0].content
        assert SUMMARY in calls[-1][0][-1].parts[0].content
        # A genuine later response replaces the estimate with measured usage.
        second.messages.append(
            ModelResponse(parts=[TextPart("ok")], usage=RequestUsage(input_tokens=99))
        )
        assert context_label("test:local", second.messages) == " · ctx: 99/100k"

    asyncio.run(run())


def test_small_history_is_noop_without_model_request():
    async def run():
        calls = []
        source = [ModelRequest(parts=[UserPromptPart("Hello")])]
        result = await summarize(source, model=summary_model(calls))
        assert not result.changed
        assert not calls

    asyncio.run(run())


@pytest.mark.parametrize("output", ["", "Huge summary " * 30000])
def test_invalid_or_unhelpful_summary_does_not_replace_history(output):
    async def run():
        source = history()
        original = deepcopy(source)
        with pytest.raises((CompactionError, UnexpectedModelBehavior)):
            await summarize(source, model=summary_model([], output=output))
        assert source == original

    asyncio.run(run())


@pytest.mark.parametrize("saved", [False, True])
def test_manual_checkpoint_preserves_plan_branches_restart_and_failed_next_turn(tmp_path, saved):
    async def run():
        calls = []
        session = (
            SavedSession.create("test:local", tmp_path, tmp_path / "sessions") if saved else None
        )
        runtime = AgentRuntime(Agent(summary_model(calls)), session)
        original = history()
        parent = "original"
        record = {"kind": "turn_started", "run_id": parent, "parent_id": None, "prompt": "Fix auth"}
        runtime.tree.consume(record)
        runtime.tree.consume({"kind": "turn_completed"})
        runtime.tree.nodes[parent].history = deepcopy(original)
        runtime.history = deepcopy(original)
        if session:
            session.append("turn_started", run_id=parent, parent_id=None, prompt="Fix auth")
            await session.store.save_snapshot(
                ContinuableSnapshot(run_id=parent, step_index=1, messages=original)
            )
            session.append("turn_completed", run_id=parent)
        plan = [PlanItem(content="Fix auth", status="in_progress", id="auth-task")]
        await runtime.plan_store.set_items(plan)
        result = await runtime.compact("Keep failures")
        compact_id = runtime.tree.active
        assert compact_id != parent
        assert runtime.tree.nodes[compact_id].kind == "compaction"
        assert ((compact_id, True), "") not in runtime.tree.rows()
        assert all(key != (compact_id, True) for key, _ in runtime.tree.rows())
        assert runtime.turns == 0  # A summary is not a user turn.
        assert runtime.input_tokens > 0
        compacted = deepcopy(result.messages)
        await runtime.navigate(parent)
        assert runtime.history == original
        await runtime.navigate(compact_id)
        assert runtime.history == compacted
        assert (await runtime.plan_store.get_items())[0].id == "auth-task"
        assert len(calls) == 1  # Navigation never calls models or tools.
        if session:
            identity, root = session.info.id, session.directory.parent
            runtime.close()
            reopened = SavedSession.open(identity, root)
            runtime = AgentRuntime(Agent(summary_model(calls)), reopened)
            await runtime.restore()
            assert runtime.history == compacted
            assert (await runtime.plan_store.get_items())[0].id == "auth-task"

        async def failing(messages, info):
            raise RuntimeError("provider down")
            yield "unreachable"

        runtime.replace_agent(Agent(FunctionModel(stream_function=failing)))
        with pytest.raises(RuntimeError, match="provider down"):
            async for _ in runtime.stream("Continue"):
                pass
        assert runtime.history == compacted
        await runtime.navigate(parent)
        assert runtime.history == original
        runtime.close()

    asyncio.run(run())


def test_manual_cancellation_and_persistence_failure_leave_context_selected(tmp_path, monkeypatch):
    async def run():
        started = asyncio.Event()

        async def slow(messages, info):
            started.set()
            await asyncio.Event().wait()
            yield SUMMARY

        session = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
        runtime = AgentRuntime(Agent(FunctionModel(stream_function=slow)), session)
        runtime.history = history()
        original = deepcopy(runtime.history)
        task = asyncio.create_task(runtime.compact())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.history == original and runtime.tree.active is None
        runtime.replace_agent(Agent(summary_model([])))

        def fail(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(session, "append", fail)
        with pytest.raises(OSError, match="disk full"):
            await runtime.compact()
        assert runtime.history == original and runtime.tree.active is None
        runtime.close()

    asyncio.run(run())


def test_auto_compacts_inside_tool_loop_and_saves_before_next_request(tmp_path, monkeypatch):
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "16000")

    async def run():
        summaries = []
        main_calls = []
        effects = []
        session = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")

        async def stream(messages, info):
            if "context summarization assistant" in (info.instructions or ""):
                summaries.append(deepcopy(messages))
                assert not info.function_tools
                yield SUMMARY
            elif not main_calls:
                main_calls.append(deepcopy(messages))
                yield {0: DeltaToolCall(name="read", json_args="{}", tool_call_id="tool-1")}
            else:
                main_calls.append(deepcopy(messages))
                assert "Tests failed; not yet fixed." in str(messages)
                assert is_provider_valid(messages)
                snapshot = await session.store.latest_snapshot(run_id=runtime.tree.active)
                assert "Tests failed; not yet fixed." in str(snapshot.messages)
                raise RuntimeError("next request failed")

        agent = Agent(FunctionModel(stream_function=stream))

        @agent.tool_plain
        def read() -> str:
            effects.append("read")
            return "diagnostic result " * 5000

        runtime = AgentRuntime(agent, session)
        runtime.auto_compact = True
        notices = []
        runtime.compaction_notice = notices.append
        with pytest.raises(RuntimeError, match="next request failed"):
            async for _ in runtime.stream("Find auth bug"):
                pass
        assert effects == ["read"]
        assert len(summaries) == 1
        assert len(main_calls) == 2
        assert "Tests failed; not yet fixed." in str(runtime.history)
        assert any("automatically" in text for text in notices)
        runtime.close()

    asyncio.run(run())


def test_window_override_and_unknown_policy(monkeypatch, tmp_path):
    monkeypatch.delenv("PCODE_CONTEXT_WINDOW", raising=False)
    assert effective_window("proxy:unknown") is None
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "32000")
    assert effective_window("proxy:unknown") == 32000
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "bogus")
    with pytest.raises(CompactionError, match="positive token"):
        effective_window("proxy:unknown")
    assert not any(isinstance(c, ClearToolResults) for c in create_coder(tmp_path).capabilities)


def test_marker_survives_suffix_growth():
    messages = history()
    messages[-1].metadata = {MARKER: {"tokens": 1234}}
    messages.append(ModelRequest(parts=[UserPromptPart("New input " * 100)]))
    assert 1234 < context_estimate(messages) < 50000


@pytest.mark.parametrize("cancel", [False, True])
def test_unsaved_auto_failure_retains_settled_tool_effects(monkeypatch, cancel):
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "16000")

    async def run():
        started = asyncio.Event()
        effects = []
        main = []

        async def stream(messages, info):
            if "context summarization assistant" in (info.instructions or ""):
                started.set()
                if cancel:
                    await asyncio.Event().wait()
                raise CompactionError("summary service unavailable")
            main.append(messages)
            yield {0: DeltaToolCall(name="edit", json_args="{}", tool_call_id="edit-1")}

        agent = Agent(FunctionModel(stream_function=stream))

        @agent.tool_plain
        def edit():
            effects.append("changed file")
            return "edited file diagnostics " * 5000

        runtime = AgentRuntime(agent)
        runtime.auto_compact = True

        async def turn():
            async for _ in runtime.stream("Fix auth"):
                pass

        task = asyncio.create_task(turn())
        await started.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else CompactionError):
            await task
        assert effects == ["changed file"]
        assert "edited file diagnostics" in str(runtime.history)
        assert "Fix auth" in str(runtime.history)
        assert is_provider_valid(runtime.history)
        selected = runtime.tree.active
        await runtime.navigate(None)
        await runtime.navigate(selected)
        assert "edited file diagnostics" in str(runtime.history)
        runtime.auto_compact = False
        runtime.replace_agent(Agent(summary_model([])))
        async for _ in runtime.stream("Continue from the completed edit"):
            pass
        assert effects == ["changed file"]

    asyncio.run(run())


def test_auto_success_survives_restart_and_does_not_recompact_stale_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "16000")

    async def run():
        summaries = []
        main = []

        async def stream(messages, info):
            if "context summarization assistant" in (info.instructions or ""):
                summaries.append(messages)
                yield SUMMARY
            else:
                main.append(deepcopy(messages))
                yield "continuing from summary"

        saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
        runtime = AgentRuntime(Agent(FunctionModel(stream_function=stream)), saved)
        runtime.history = history()
        runtime.auto_compact = True
        for prompt in ("Continue", "What next?"):
            async for _ in runtime.stream(prompt):
                pass
        assert len(summaries) == 1
        assert len(main) == 2
        assert "Tests failed; not yet fixed." in str(runtime.history)
        assert runtime.input_tokens > 0
        compacted = deepcopy(runtime.history)
        expected_usage = (runtime.input_tokens, runtime.output_tokens)
        identity, root = saved.info.id, saved.directory.parent
        runtime.close()
        reopened = AgentRuntime(Agent(summary_model([])), SavedSession.open(identity, root))
        await reopened.restore()
        assert reopened.history == compacted
        assert (reopened.input_tokens, reopened.output_tokens) == expected_usage
        reopened.close()

    asyncio.run(run())


def test_schema_growth_is_added_to_existing_external_overhead():
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.tools import ToolDefinition

    from pcode.compaction import SCHEMAS, schema_tokens

    parameters = ModelRequestParameters(
        function_tools=[ToolDefinition(name="new_mcp_tool", description="large schema " * 1500)]
    )
    messages = [
        ModelResponse(
            parts=[TextPart("Short message")],
            usage=RequestUsage(input_tokens=10000),
            metadata={MARKER: {"tokens": 10000, "schema_tokens": 1000}},
        )
    ]
    assert context_estimate(messages, parameters) == 10000 + schema_tokens(parameters) - 1000
    messages[-1].metadata = {SCHEMAS: 1000}
    assert context_estimate(messages, parameters) == 10000 + schema_tokens(parameters) - 1000


def test_override_display_and_safe_compaction_errors(monkeypatch):
    from pcode.live import error_message

    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "32000")
    assert context_label("proxy:unknown", []) == " · ctx: 0/32k"
    assert context_label("anthropic:claude-sonnet-4-6", []) == " · ctx: 0/32k"
    error = CompactionError("Not enough room. Use /compact with a focus or /new.")
    assert error_message(error) == str(error)
