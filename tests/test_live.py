import asyncio
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.capabilities import CombinedCapability
from pydantic_ai.messages import RetryPromptPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness import Coder
from rich.console import Console

from pcode.agent import create_agent, create_coder
from pcode.app import PreviewApp
from pcode.live import AgentRuntime, error_message
from pcode.runtime import Message, RunStatus, TextDelta, ToolStarted, ToolSummary
from pcode.ui import TerminalOutput, create_prompt


def test_codex_uses_native_model_with_only_cache_override(tmp_path):
    with patch("pcode.agent.Agent") as constructor, patch("pcode.agent.OpenAICodexModel") as model:
        create_agent("openai-codex:gpt-5.6-luna", tmp_path)
    model.assert_called_once_with(
        "gpt-5.6-luna", profile={"openai_supports_prompt_cache_breakpoints": False}
    )
    assert constructor.call_args.args == (model.return_value,)
    assert isinstance(constructor.call_args.kwargs["capabilities"][0], CombinedCapability)


def test_other_model_strings_are_passed_unchanged(tmp_path):
    with patch("pcode.agent.Agent") as constructor:
        create_agent("openai:example", tmp_path)
    assert constructor.call_args.args == ("openai:example",)


def test_stream_runs_real_coder_read_tool_and_retains_history(tmp_path):
    (tmp_path / "sample.txt").write_text("a unique workspace marker")
    requests = []

    async def model(messages, info):
        requests.append(messages)
        names = {tool.name for tool in info.function_tools}
        assert {"read_file", "edit_file", "run_command"} <= names
        if len(requests) == 1:
            yield "Looking at the file."
            yield {0: DeltaToolCall(name="read_file", json_args='{"path":"sample.txt"}')}
        elif len(requests) == 2:
            results = [
                p for message in messages for p in message.parts if isinstance(p, ToolReturnPart)
            ]
            assert any("a unique workspace marker" in str(p.content) for p in results)
            yield "The file contains "
            yield "the workspace marker."
        else:
            assert len(messages) > len(requests[0])
            yield "I remember the previous answer."

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[Coder(tmp_path)])
    )

    async def run():
        events = [event async for event in runtime.stream("Read sample.txt")]
        started = next(event for event in events if isinstance(event, ToolStarted))
        completed = next(event for event in events if isinstance(event, ToolSummary))
        assert started.name == "read_file"
        assert started.detail == "sample.txt"
        assert started.call_id == completed.call_id
        assert events.index(started) < events.index(completed)

        assert [e.markdown for e in events if isinstance(e, Message)] == [
            "Looking at the file.",
            "The file contains the workspace marker.",
        ]
        assert any(isinstance(e, TextDelta) for e in events)
        assert any(isinstance(e, RunStatus) and "read_file" in e.text for e in events)
        assert any(isinstance(e, ToolSummary) and e.name == "read_file" for e in events)
        assert runtime.turns == 1
        assert runtime.history
        assert runtime.input_tokens > 0
        followup = [event async for event in runtime.stream("What did you read?")]
        assert Message("I remember the previous answer.") in followup
        assert runtime.turns == 2
        old_id = runtime.conversation_id
        runtime.reset()
        assert runtime.history == []
        assert runtime.turns == 0
        assert runtime.conversation_id != old_id

    asyncio.run(run())


@pytest.mark.parametrize("outside_path", ["absolute", "relative", "symlink"])
def test_coder_tools_are_scoped_to_the_workspace(tmp_path, outside_path):
    import json

    from pydantic_ai_harness.filesystem import FileSystem
    from pydantic_ai_harness.repo_context import RepoContext
    from pydantic_ai_harness.shell import Shell

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside marker")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside marker")
    (workspace / "link.txt").symlink_to(outside)
    requested_path = {
        "absolute": str(outside),
        "relative": "../outside.txt",
        "symlink": "link.txt",
    }[outside_path]
    coder = create_coder(workspace)
    assert next(c for c in coder.capabilities if isinstance(c, Shell)).cwd == workspace
    context = next(c for c in coder.capabilities if isinstance(c, RepoContext))
    assert context.workspace_dir == workspace
    filesystem = next(c for c in coder.capabilities if isinstance(c, FileSystem))
    # File tools enforce their workspace root without prompt-level path guidance.
    # This does not confine shell commands.
    assert Path(filesystem.root_dir) == workspace
    assert filesystem.protected_patterns
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {
                0: DeltaToolCall(name="read_file", json_args=json.dumps({"path": requested_path}))
            }
        elif requests == 2:
            parts = [p for message in messages for p in message.parts]
            # The sibling read is refused by the capability, not by prompt text.
            assert not any("outside marker" in str(getattr(p, "content", "")) for p in parts)
            retries = [p for p in parts if isinstance(p, RetryPromptPart)]
            assert any("outside the root directory" in str(p.content) for p in retries)
            yield {0: DeltaToolCall(name="read_file", json_args=json.dumps({"path": "inside.txt"}))}
        else:
            results = [
                p for message in messages for p in message.parts if isinstance(p, ToolReturnPart)
            ]
            assert any("inside marker" in str(p.content) for p in results)
            yield "Finished"

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model), capabilities=[coder]))

    async def run():
        events = [event async for event in runtime.stream("Read inside and outside the repository")]
        assert Message("Finished") in events

    asyncio.run(run())


def test_coder_allows_all_commands_by_default(tmp_path):
    from pydantic_ai_harness.shell import Shell
    from pydantic_ai_harness.shell._capability import LLM_API_KEY_ENV_PATTERNS

    coder = create_coder(tmp_path)
    shell = next(c for c in coder.capabilities if isinstance(c, Shell))
    assert not shell.allowed_commands
    assert not shell.denied_commands
    assert not shell.denied_operators
    assert shell.allow_interactive
    assert shell.denied_env_patterns == LLM_API_KEY_ENV_PATTERNS

    async def run():
        # Previously excluded executables, without inspecting real environment values.
        toolset = shell.get_toolset()
        assert "allowed" in await toolset.run_command("printf allowed")
        assert "allowed" in await toolset.run_command("python3 -c 'print(\"allowed\")'")

    asyncio.run(run())


def test_stream_has_no_model_request_limit():
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        if requests <= 55:
            yield {0: DeltaToolCall(name="noop", json_args="{}")}
        else:
            yield "Finished beyond both the old and library-default caps."

    agent = Agent(FunctionModel(stream_function=model))

    @agent.tool_plain
    def noop() -> str:
        return "ok"

    runtime = AgentRuntime(agent)

    async def run():
        events = [event async for event in runtime.stream("Run a long tool loop")]
        assert Message("Finished beyond both the old and library-default caps.") in events
        assert requests == 56
        assert runtime.turns == 1

    asyncio.run(run())


def test_failed_stream_does_not_commit_history():
    async def broken(messages, info):
        yield "Partial"
        raise RuntimeError("sensitive provider body")

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=broken)))

    async def run():
        with pytest.raises(Exception):
            _ = [event async for event in runtime.stream("hello")]
        assert runtime.history == []
        assert runtime.turns == 0

    asyncio.run(run())
    assert "sensitive provider body" not in error_message(RuntimeError("sensitive provider body"))


def test_ui_stream_commits_final_message_only_once():
    async def model(messages, info):
        yield "hello "
        yield "from the model"

    output = StringIO()
    app = PreviewApp(
        model="test:local",
        runtime=AgentRuntime(Agent(FunctionModel(stream_function=model))),
        console=Console(file=output, color_system=None),
    )

    async def run():
        with create_pipe_input() as pipe:
            session = create_prompt(
                app.registry, activity=app.activity, input=pipe, output=DummyOutput()
            )
            writer = TerminalOutput(app.transcript.console, app.activity, session.app)
            app.transcript.output = writer
            assert await asyncio.wait_for(app.run_live(writer, "hello"), timeout=5)
            await writer.flush()
        assert not app.activity.busy
        assert output.getvalue().count("hello from the model") == 1
        assert app.runtime.turns == 1

    asyncio.run(run())


def test_ui_cancellation_cleans_up_generation_and_accepts_next_input():
    output = StringIO()
    cleaned_up = []

    async def run():
        started = asyncio.Event()

        async def model(messages, info):
            try:
                yield "unfinished answer"
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaned_up.append(True)

        app = PreviewApp(
            model="test:local",
            runtime=AgentRuntime(Agent(FunctionModel(stream_function=model))),
            console=Console(file=output, color_system=None),
        )
        with create_pipe_input() as pipe:
            session = create_prompt(
                app.registry, activity=app.activity, input=pipe, output=DummyOutput()
            )
            writer = TerminalOutput(app.transcript.console, app.activity, session.app)
            app.transcript.output = writer
            task = asyncio.create_task(app.run_live(writer, "start"))
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            assert not await asyncio.wait_for(task, timeout=5)
            await writer.flush()
            assert cleaned_up
            assert not app.activity.busy
            assert app.runtime.history == []
            pipe.send_text("next input\r")
            assert await session.prompt_async() == "next input"
        assert "Run cancelled" in output.getvalue()

    asyncio.run(run())
