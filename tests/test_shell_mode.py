"""`!command` at the prompt: runs locally, reaches the model as a shell tool exchange."""

import asyncio

import pytest
from pydantic_ai import Agent, ToolReturn
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from pcode.live import AgentRuntime
from pcode.runtime import Message
from pcode.shell_mode import ShellRun, execute, shell_command, shell_exchange
from pcode.tool_output_limits import create_tool_output_limits


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def parts(messages, kind):
    return [p for m in messages for p in m.parts if isinstance(p, kind)]


def echo_model(seen):
    async def model(messages, info):
        seen.append(messages)
        yield "Finished"

    return FunctionModel(stream_function=model)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("!make test", "make test"),
        ("  ! ls -la  ", "ls -la"),
        ("!", None),
        ("!!", None),
        ("!!last", None),
        ("hello", None),
        ("/help", None),
        ("say !hi", None),
    ],
)
def test_shell_command_parsing(text, expected):
    assert shell_command(text) == expected


def test_execute_streams_combined_output_and_exit_code(tmp_path):
    chunks = []

    async def run():
        return await execute(
            "printf out; printf err >&2; exit 3", cwd=tmp_path, on_output=chunks.append
        )

    run = asyncio.run(run())
    assert run.output == "outerr"
    assert run.exit_code == 3
    assert run.failed
    assert "".join(chunks) == "outerr"
    assert run.tool_result() == "outerr\n[exit code: 3]"
    assert ShellRun("x", "fine", 0, 0.1).tool_result() == "fine"


def test_execute_cancellation_kills_the_process_group(tmp_path):
    marker = tmp_path / "survived"

    async def run():
        task = asyncio.create_task(
            execute(f"sleep 30; touch {marker}", cwd=tmp_path, on_output=lambda _: None)
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert not marker.exists()


def test_shell_exchange_shapes():
    later = shell_exchange("ls", "a\nb", call_id="c1")
    assert [type(m) for m in later] == [ModelResponse, ModelRequest]
    call = later[0].parts[0]
    assert isinstance(call, ToolCallPart)
    assert call.tool_name == "shell" and call.tool_call_id == "c1"
    assert call.args == {"command": "ls"}
    result = later[1].parts[0]
    assert isinstance(result, ToolReturnPart)
    assert result.content == "a\nb" and result.tool_call_id == "c1"

    first = shell_exchange("ls", "a", call_id="c1", first=True)
    assert isinstance(first[0], ModelRequest)
    assert first[0].parts[0].content == "!ls"

    stored = ToolReturn(return_value="short", metadata={"h": 1})
    spilled = shell_exchange("ls", stored, call_id="c2")
    assert spilled[1].parts[0].content == "short"
    assert spilled[1].parts[0].metadata == {"h": 1}


def test_recorded_command_reaches_the_model_with_the_next_prompt():
    seen = []
    runtime = AgentRuntime(Agent(echo_model(seen)))

    async def run():
        visible = await runtime.record_shell(ShellRun("make test", "1 passed", 0, 0.5))
        assert visible == "1 passed"
        assert runtime.history == []
        events = [event async for event in runtime.stream("What failed?")]
        assert Message("Finished") in events

    asyncio.run(run())
    request = seen[0]
    # First turn: the typed line opens the conversation so a provider never
    # sees an assistant message first; then the call, its result, and the prompt.
    prompts = parts(request, UserPromptPart)
    assert [p.content for p in prompts] == ["!make test", "What failed?"]
    calls = parts(request, ToolCallPart)
    assert len(calls) == 1 and calls[0].args == {"command": "make test"}
    returns = parts(request, ToolReturnPart)
    assert len(returns) == 1 and returns[0].content == "1 passed"
    assert returns[0].tool_call_id == calls[0].tool_call_id
    # Tool results precede the user's prompt in the merged request.
    last = request[-1]
    assert isinstance(last.parts[0], ToolReturnPart)
    assert isinstance(last.parts[-1], UserPromptPart)
    # The exchange now belongs to history and is not sent again.
    assert runtime.pending_shell == []
    assert len(parts(runtime.history, ToolCallPart)) == 1


def test_recorded_command_mid_conversation_adds_no_user_turn():
    seen = []
    runtime = AgentRuntime(Agent(echo_model(seen)))

    async def run():
        _ = [event async for event in runtime.stream("hello")]
        await runtime.record_shell(ShellRun("ls", "a", 0, 0.1))
        await runtime.record_shell(ShellRun("pwd", "/x", 0, 0.1))
        _ = [event async for event in runtime.stream("and?")]

    asyncio.run(run())
    request = seen[1]
    assert [p.content for p in parts(request, UserPromptPart)] == ["hello", "and?"]
    assert [c.args["command"] for c in parts(request, ToolCallPart)] == ["ls", "pwd"]
    # A ModelResponse never directly follows another: each call has its result.
    kinds = [type(m).__name__ for m in request]
    assert kinds[-4:] == ["ModelResponse", "ModelRequest", "ModelResponse", "ModelRequest"]


def test_failed_first_request_does_not_queue_the_exchange_twice():
    attempts = 0

    async def flaky(messages, info):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("provider down")
        assert len(parts(messages, ToolCallPart)) == 1
        yield "ok"

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=flaky)))

    async def run():
        await runtime.record_shell(ShellRun("ls", "a", 0, 0.1))
        with pytest.raises(RuntimeError):
            _ = [event async for event in runtime.stream("hi")]
        # The request checkpoint kept the exchange, so it is history now.
        assert runtime.pending_shell == []
        assert len(parts(runtime.history, ToolCallPart)) == 1
        _ = [event async for event in runtime.stream(None)]

    asyncio.run(run())
    assert attempts == 2


def test_app_runs_command_shows_output_and_queues_it_for_the_model(tmp_path):
    from io import StringIO

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.ui import TerminalOutput, create_prompt

    seen = []
    output = StringIO()
    app = PreviewApp(
        model="test:local",
        runtime=AgentRuntime(Agent(echo_model(seen))),
        console=Console(file=output, color_system=None),
        workspace=tmp_path,
    )

    async def run():
        with create_pipe_input() as pipe:
            session = create_prompt(
                app.registry, activity=app.activity, input=pipe, output=DummyOutput()
            )
            writer = TerminalOutput(app.transcript.console, session.app)
            app.transcript.output = writer
            assert await asyncio.wait_for(
                app.run_shell(writer, "!printf 'shell mode output'; exit 2"), timeout=10
            )
            await writer.flush()
            assert not app.activity.user_command
            assert not app.activity.command_outputs
            assert await asyncio.wait_for(app.run_live(writer, "what happened?"), timeout=10)
            await writer.flush()

    asyncio.run(run())
    text = output.getvalue()
    assert "shell mode output" in text
    assert "with your next message" in text
    assert app.runtime.turns == 1
    calls = parts(seen[0], ToolCallPart)
    assert calls[0].args["command"] == "printf 'shell mode output'; exit 2"
    assert parts(seen[0], ToolReturnPart)[0].content == "shell mode output\n[exit code: 2]"


def test_large_output_is_reduced_like_a_tool_result(monkeypatch):
    from pcode.config import configure

    configure(["set", "tool_output_threshold", "100"])
    configure(["set", "tool_output_preview_chars", "40"])
    limits = create_tool_output_limits()
    seen = []
    runtime = AgentRuntime(Agent(echo_model(seen), capabilities=[limits]))
    big = "line\n" * 100

    async def run():
        visible = await runtime.record_shell(ShellRun("make test", big, 0, 1.0))
        assert isinstance(visible, ToolReturn)
        assert len(visible.return_value) < len(big)
        assert "read_tool_result" in visible.return_value
        _ = [event async for event in runtime.stream("so?")]

    asyncio.run(run())
    returned = parts(seen[0], ToolReturnPart)[0]
    assert big not in str(returned.content)
    assert returned.metadata and "handle" in str(returned.metadata)
