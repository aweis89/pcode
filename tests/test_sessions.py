import asyncio
import json
import sqlite3
import stat
import subprocess
import sys

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness import Coder
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, ToolEffectRecord

from pcode.diagnostics import error_details
from pcode.live import AgentRuntime, error_message
from pcode.runtime import Message
from pcode.sessions import SavedSession, SessionError, list_sessions, resolve_session


def test_round_trip_session_keeps_model_messages_and_transcript(tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    identity = saved.info.id
    requests = []

    async def model(messages, info):
        requests.append(messages)
        yield "A saved answer."

    agent = Agent(FunctionModel(stream_function=model))

    async def run():
        runtime = AgentRuntime(agent, saved)
        _ = [event async for event in runtime.stream("first question")]
        history = runtime.history
        runtime.close()
        reopened = SavedSession.open(identity[:8], root)
        try:
            restored = AgentRuntime(agent, reopened)
            await restored.restore()
            assert restored.history == history
            assert restored.turns == 1
            assert restored.conversation_id == identity
            _ = [event async for event in restored.stream("follow up")]
            assert len(requests[-1]) > len(requests[0])
            assert restored.turns == 2
            records = list(reopened.transcript_records())
            assert sum(r["kind"] == "Message" for r in records) == 2
            assert [r["prompt"] for r in records if r["kind"] == "turn_started"] == [
                "first question",
                "follow up",
            ]
        finally:
            reopened.close()

    try:
        asyncio.run(run())
        assert resolve_session("latest", root).name == identity
        assert list_sessions(root)[0].turns == 2
        check = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import asyncio, sys; from pathlib import Path; "
                    "from pcode.sessions import SavedSession; "
                    "s=SavedSession.open(sys.argv[1], Path(sys.argv[2])); "
                    "print(len(asyncio.run(s.recover()))); s.close()"
                ),
                identity,
                str(root),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert int(check.stdout.strip()) > 0
        assert stat.S_IMODE((root / identity).stat().st_mode) == 0o700
        for name in ("session.json", "steps.sqlite3", "transcript.jsonl"):
            assert stat.S_IMODE((root / identity / name).stat().st_mode) == 0o600
    finally:
        saved.close()


def test_http_failure_after_tool_keeps_tool_result_and_diagnostics(tmp_path, monkeypatch):
    marker = "private-test-api-value-should-not-be-in-errors"
    monkeypatch.setenv("EXAMPLE_API_KEY", marker)
    (tmp_path / "README.md").write_text("a test repository")
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {0: DeltaToolCall(name="read_file", json_args='{"path":"README.md"}')}
        elif calls == 2:
            raise ModelHTTPError(
                400,
                "test:local",
                body={
                    "error": {
                        "message": f"Unsupported request parameter; api_key={marker}",
                        "param": "prompt_cache_breakpoint",
                        "code": "invalid_parameter",
                    }
                },
            )
        else:
            results = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            assert any("a test repository" in str(p.content) for p in results)
            yield "Continued without calling the file tool again."

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[Coder(tmp_path)]), saved
    )

    async def run():
        with pytest.raises(ModelHTTPError) as raised:
            _ = [event async for event in runtime.stream("Read the README")]
        assert "Unsupported request parameter" in error_message(raised.value)
        assert marker not in error_message(raised.value)
        assert runtime.history
        assert saved.info.status == "failed"
        records = list(saved.transcript_records())
        failure = next(r for r in records if r["kind"] == "turn_failed")
        assert failure["error"]["provider_param"] == "prompt_cache_breakpoint"
        assert marker not in json.dumps(failure)
        with sqlite3.connect(saved.directory / "steps.sqlite3") as connection:
            errors = connection.execute(
                "SELECT error FROM events WHERE error IS NOT NULL"
            ).fetchall()
        assert errors and marker not in str(errors)
        runtime.close()
        reopened = SavedSession.open(saved.info.id, saved.directory.parent)
        recovered = AgentRuntime(runtime.agent, reopened)
        try:
            await recovered.restore()
            events = [event async for event in recovered.stream("Continue")]
            assert Message("Continued without calling the file tool again.") in events
            assert calls == 3
        finally:
            recovered.close()

    try:
        asyncio.run(run())
    finally:
        saved.close()


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file", "run_command", "unknown_tool"])
@pytest.mark.parametrize("journaled", [False, True])
def test_resume_without_checkpoint_allows_interrupted_tools(tmp_path, tool_name, journaled):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")

    async def run():
        await saved.store.register_run(RunRecord(run_id="crashed", conversation_id=saved.info.id))
        await saved.store.record_tool_effect(
            ToolEffectRecord(
                run_id="crashed",
                tool_call_id="call-1",
                tool_name=tool_name,
                status="started",
            )
        )
        if journaled:
            saved.append("turn_started", run_id="crashed", prompt="interrupted turn")
        assert await saved.recover() == []
        effects = await saved.store.list_unresolved_tool_effects(run_id="crashed")
        assert len(effects) == 1
        assert effects[0].status == "started"

    try:
        asyncio.run(run())
    finally:
        saved.close()


def test_session_lock_and_path_validation(tmp_path):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        with pytest.raises(SessionError, match="already open"):
            SavedSession.open(saved.info.id, saved.directory.parent)
        with pytest.raises(SessionError, match="Use a session ID"):
            resolve_session("../../outside", saved.directory.parent)
    finally:
        saved.close()
    reopened = SavedSession.open(saved.info.id, saved.directory.parent)
    reopened.close()


def test_latest_is_scoped_to_the_given_workspace(tmp_path):
    root = tmp_path / "sessions"
    here, elsewhere = tmp_path / "here", tmp_path / "elsewhere"
    here.mkdir()
    elsewhere.mkdir()
    mine = SavedSession.create("test:local", here, root)
    mine.close()
    # Newer, but belongs to another checkout.
    other = SavedSession.create("test:local", elsewhere, root)
    other.close()

    assert resolve_session("latest", root).name == other.info.id
    assert resolve_session("latest", root, here).name == mine.info.id
    assert resolve_session("latest", root, elsewhere).name == other.info.id
    with pytest.raises(SessionError, match="No saved session for"):
        resolve_session("latest", root, tmp_path)


@pytest.mark.parametrize("tail", [b'{"kind":"TextDelta","text":"torn', b'{"text":"\xf0\x9f'])
def test_torn_journal_does_not_hide_future_events(tmp_path, tail):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        saved.event(Message("before crash"))
        with (saved.directory / "transcript.jsonl").open("ab") as file:
            file.write(tail)
        saved.event(Message("after restart"))
        assert [r["markdown"] for r in saved.transcript_records()] == [
            "before crash",
            "after restart",
        ]
    finally:
        saved.close()


def test_new_session_preserves_old_one(tmp_path):
    async def model(messages, info):
        yield "answer"

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)), saved)
    identity = saved.info.id
    runtime.reset()
    try:
        assert runtime.session is None
        assert runtime.history == []
        assert len(list_sessions(saved.directory.parent)) == 1
        reopened = SavedSession.open(identity, saved.directory.parent)
        reopened.close()

        async def submit():
            return [event async for event in runtime.stream("new question")]

        asyncio.run(submit())
        assert runtime.session.info.id != identity
        assert runtime.conversation_id == runtime.session.info.id
        assert len(list_sessions(saved.directory.parent)) == 2
    finally:
        runtime.close()


def test_diagnostics_redact_provider_secrets_and_keep_failure_reason(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "example-token-with-sensitive-value")
    error = ModelHTTPError(
        400,
        "example",
        body={
            "error": {
                "message": (
                    "Bad parameter; Bearer example-token-with-sensitive-value; refresh_token=hidden"
                ),
                "code": "invalid_parameter",
                "param": "prompt_cache_breakpoint",
                "headers": {"Authorization": "must not be recorded"},
            }
        },
    )
    detail = error_details(error)
    encoded = json.dumps(detail)
    assert "example-token-with-sensitive-value" not in encoded
    assert "hidden" not in encoded
    assert "must not be recorded" not in encoded
    assert detail["provider_param"] == "prompt_cache_breakpoint"


@pytest.mark.parametrize(
    "tool_name",
    [
        "read_file",
        "list_directory",
        "search_files",
        "find_files",
        "file_info",
        "read_tool_result",
        "write_file",
        "edit_file",
        "run_command",
        "unknown_tool",
    ],
)
@pytest.mark.parametrize("checkpoint_in_prior_run", [False, True])
@pytest.mark.parametrize("journaled", [False, True])
def test_resume_abandons_interrupted_tools_without_replaying(
    tmp_path, tool_name, checkpoint_in_prior_run, journaled
):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")

    async def run():
        if journaled:
            saved.append("turn_started", run_id="prior", prompt="prior turn")
            saved.append("turn_completed")
            saved.append("turn_started", run_id="crashed", prompt="interrupted turn")
        history = [ModelRequest(parts=[UserPromptPart("saved checkpoint")])]
        await saved.store.register_run(RunRecord(run_id="prior", conversation_id=saved.info.id))
        await saved.store.register_run(RunRecord(run_id="crashed", conversation_id=saved.info.id))
        await saved.store.save_snapshot(
            ContinuableSnapshot(
                run_id="prior" if checkpoint_in_prior_run else "crashed",
                step_index=0,
                messages=history,
            )
        )
        await saved.store.save_snapshot(
            ContinuableSnapshot(
                run_id="crashed",
                step_index=1,
                state="interrupted",
                messages=[
                    *history,
                    ModelResponse(parts=[ToolCallPart(tool_name, {}, "call-1")]),
                ],
            )
        )
        await saved.store.record_tool_effect(
            ToolEffectRecord(
                run_id="crashed",
                tool_call_id="call-1",
                tool_name=tool_name,
                status="started",
            )
        )
        assert await saved.recover() == history
        # Recovery must not rewrite the historical effect as successful.
        effects = await saved.store.list_unresolved_tool_effects(run_id="crashed")
        assert len(effects) == 1
        assert effects[0].status == "started"
        # Mixed batches also resume without rewriting any unresolved effects.
        await saved.store.record_tool_effect(
            ToolEffectRecord(
                run_id="crashed",
                tool_call_id="write-1",
                tool_name="write_file",
                status="started",
            )
        )
        assert await saved.recover() == history
        effects = await saved.store.list_unresolved_tool_effects(run_id="crashed")
        assert {effect.tool_call_id: effect.status for effect in effects} == {
            "call-1": "started",
            "write-1": "started",
        }

    try:
        asyncio.run(run())
    finally:
        saved.close()


def test_reopened_session_continues_without_replaying_interrupted_write(tmp_path):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    requests = []
    tool_calls = []

    async def model(messages, info):
        requests.append(messages)
        yield "Ready to continue."

    agent = Agent(FunctionModel(stream_function=model))

    @agent.tool_plain
    def write_file(path: str, content: str) -> str:
        tool_calls.append(path)
        return "written"

    async def run():
        runtime = AgentRuntime(agent, saved)
        _ = [event async for event in runtime.stream("first question")]
        history = list(runtime.history)
        await saved.store.register_run(RunRecord(run_id="crashed", conversation_id=saved.info.id))
        saved.append("turn_started", run_id="crashed", prompt="interrupted write")
        await saved.store.save_snapshot(
            ContinuableSnapshot(
                run_id="crashed",
                step_index=0,
                state="interrupted",
                messages=[
                    *history,
                    ModelResponse(
                        parts=[
                            ToolCallPart(
                                "write_file", {"path": "file.txt", "content": "test"}, "write-1"
                            )
                        ]
                    ),
                ],
            )
        )
        effect = ToolEffectRecord(
            run_id="crashed", tool_call_id="write-1", tool_name="write_file", status="started"
        )
        await saved.store.record_tool_effect(effect)
        runtime.close()
        reopened = SavedSession.open(saved.info.id, saved.directory.parent)
        restored = AgentRuntime(agent, reopened)
        try:
            await restored.restore()
            assert restored.history == history
            events = [event async for event in restored.stream("Continue")]
            assert Message("Ready to continue.") in events
            assert len(requests) == 2
            assert not any(
                isinstance(part, ToolCallPart) for message in requests[-1] for part in message.parts
            )
            assert tool_calls == []
            assert (
                await reopened.store.get_tool_effect(run_id="crashed", tool_call_id="write-1")
                == effect
            )
        finally:
            restored.close()

    try:
        asyncio.run(run())
    finally:
        saved.close()


@pytest.mark.parametrize("outcome", ["done", "error", "cancel"])
def test_thinking_persists_and_replays_after_reopen_even_when_hidden(tmp_path, outcome):
    from io import StringIO

    from pydantic_ai.models.function import DeltaThinkingPart
    from rich.console import Console

    from pcode.app import PreviewApp

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    identity = saved.info.id
    # More than the old 8-KB live preview limit, with a distinctive beginning/end.
    thought = "FIRST_THOUGHT\n" + "thinking text\n" * 1000 + "LAST_THOUGHT"

    async def model(messages, info):
        yield {0: DeltaThinkingPart(content=thought[:100])}
        yield {0: DeltaThinkingPart(content=thought[100:], signature="OPAQUE_SIGNATURE")}
        if outcome == "error":
            raise RuntimeError("deliberate model failure")
        if outcome == "cancel":
            raise asyncio.CancelledError
        yield "Public answer"

    async def run():
        agent = Agent(FunctionModel(stream_function=model))
        runtime = AgentRuntime(agent, saved)
        try:
            try:
                _ = [event async for event in runtime.stream("Question")]
            except (RuntimeError, asyncio.CancelledError):
                assert outcome != "done"
        finally:
            runtime.close()
        reopened = SavedSession.open(identity, root)
        runtime = AgentRuntime(agent, reopened)
        try:
            records = list(reopened.transcript_records())
            thinking = [r for r in records if r["kind"] in ("Thinking", "thinking_partial")]
            assert len(thinking) == 1
            assert thinking[0]["text"] == thought
            assert "OPAQUE_SIGNATURE" not in repr(list(reopened.records()))
            app = PreviewApp(model="test:local", runtime=runtime, console=Console(file=StringIO()))
            app.activity.show_thinking = False
            app.replay()
            assert "FIRST_THOUGHT" not in repr(app.transcript.replay())
            app.set_show_thinking(True)
            replay = repr(app.transcript.replay())
            assert replay.count("FIRST_THOUGHT") == 1
            assert replay.count("LAST_THOUGHT") == 1
            app.set_show_thinking(False)
            assert "FIRST_THOUGHT" not in repr(app.transcript.replay())
        finally:
            runtime.close()

    asyncio.run(run())


def test_thinking_completion_does_not_repeat_deltas_across_interleaved_tool_records():
    from types import SimpleNamespace

    records = [
        {"kind": "ThinkingDelta", "text": "first"},
        {"kind": "ToolSummary", "name": "read_file"},
        {"kind": "ThinkingDelta", "text": " second"},
        {"kind": "Thinking", "text": "first second"},
        {"kind": "Message", "markdown": "Answer"},
        {"kind": "ThinkingDelta", "text": "interrupted"},
        {"kind": "turn_cancelled"},
    ]
    saved = SimpleNamespace(active_records=lambda: iter(records))
    assert list(SavedSession.transcript_records(saved)) == [
        {"kind": "thinking_partial", "text": "first"},
        {"kind": "ToolSummary", "name": "read_file"},
        {"kind": "thinking_partial", "text": " second"},
        {"kind": "Message", "markdown": "Answer"},
        {"kind": "thinking_partial", "text": "interrupted"},
        {"kind": "turn_cancelled"},
    ]


def test_resumed_history_draws_without_a_runtime(tmp_path):
    """The journal is a millisecond read; the provider stack behind the runtime is seconds."""
    from io import StringIO

    from rich.console import Console

    from pcode.app import PreviewApp

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    identity = saved.info.id

    async def model(messages, info):
        yield "SAVED_ANSWER"

    async def run():
        runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)), saved)
        try:
            _ = [event async for event in runtime.stream("SAVED_QUESTION")]
        finally:
            runtime.close()

    asyncio.run(run())
    reopened = SavedSession.open(identity, root)
    try:
        # No runtime at all: the offline preview stands in until the real one
        # finishes importing, exactly as it does during startup.
        app = PreviewApp(
            model="test:local",
            saved_session=reopened,
            resume=True,
            console=Console(file=StringIO(), width=80, color_system=None),
        )
        assert getattr(app.runtime, "session", None) is None
        app.replay(reopened)
        stream = StringIO()
        console = Console(file=stream, width=80, color_system=None)
        for objects, end, soft_wrap in app.transcript.replay():
            console.print(*objects, end=end, soft_wrap=soft_wrap)
        shown = stream.getvalue()
        assert "SAVED_QUESTION" in shown
        assert "SAVED_ANSWER" in shown
    finally:
        reopened.close()
