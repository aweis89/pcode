import asyncio
import json
import sqlite3
import stat
import subprocess
import sys

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart
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
            records = reopened.recent_transcript()
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
        records = saved.recent_transcript()
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
def test_resume_refuses_unknown_tool_effects(tmp_path, tool_name):
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
        with pytest.raises(SessionError, match="Interrupted tool effects"):
            await saved.recover()

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


@pytest.mark.parametrize("tail", [b'{"kind":"TextDelta","text":"torn', b'{"text":"\xf0\x9f'])
def test_torn_journal_does_not_hide_future_events(tmp_path, tail):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        saved.event(Message("before crash"))
        with (saved.directory / "transcript.jsonl").open("ab") as file:
            file.write(tail)
        saved.event(Message("after restart"))
        assert [r["markdown"] for r in saved.recent_transcript()] == [
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
    ["read_file", "list_directory", "search_files", "find_files", "file_info", "read_tool_result"],
)
@pytest.mark.parametrize("checkpoint_in_prior_run", [False, True])
def test_resume_abandons_interrupted_reads_without_replaying(
    tmp_path, tool_name, checkpoint_in_prior_run
):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")

    async def run():
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
                messages=[ModelRequest(parts=[UserPromptPart("unsafe partial history")])],
            )
        )
        await saved.store.record_tool_effect(
            ToolEffectRecord(
                run_id="crashed",
                tool_call_id="read-1",
                tool_name=tool_name,
                status="started",
            )
        )
        assert await saved.recover() == history
        # Recovery must not rewrite the historical effect as successful.
        effects = await saved.store.list_unresolved_tool_effects(run_id="crashed")
        assert len(effects) == 1
        assert effects[0].status == "started"
        # A mixed batch must still block on its potentially mutating tool.
        await saved.store.record_tool_effect(
            ToolEffectRecord(
                run_id="crashed",
                tool_call_id="write-1",
                tool_name="write_file",
                status="started",
            )
        )
        with pytest.raises(SessionError, match="Interrupted tool effects"):
            await saved.recover()

    try:
        asyncio.run(run())
    finally:
        saved.close()
