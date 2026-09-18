import asyncio
import json
import shlex
import sys
from pathlib import Path
from unittest.mock import Mock

from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from pcode.agent import create_coder
from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.runtime import CommandOutput, ToolSummary
from pcode.shell import StreamingShell, preview_text


def command(source):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def runtime_for(workspace, source, timeout=5):
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                0: DeltaToolCall(
                    name="run_command",
                    json_args=json.dumps({"command": command(source), "timeout_seconds": timeout}),
                )
            }
        else:
            yield "Done."

    return AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(workspace)])
    )


def test_output_arrives_before_exit_and_final_result_is_unchanged(tmp_path):
    gate = tmp_path / "continue"
    runtime = runtime_for(
        tmp_path,
        "import pathlib, time, sys; print('FIRST'); print('ERROR', file=sys.stderr); "
        f"gate = pathlib.Path({str(gate)!r})\n"
        "while not gate.exists(): time.sleep(.02)\n"
        "print('LAST')",
    )

    runtime.tree.consume = Mock(wraps=runtime.tree.consume)

    async def run():
        events = []
        async for event in runtime.stream("run"):
            events.append(event)
            if isinstance(event, CommandOutput) and "ERROR" in event.output:
                assert not any(isinstance(e, ToolSummary) for e in events)
                assert "FIRST" in event.output
                gate.touch()
        result = next(e for e in events if isinstance(e, ToolSummary))
        preview = next(e for e in events if isinstance(e, CommandOutput))
        assert preview.call_id == result.call_id
        assert not result.failed
        assert result.result == "[stdout]\nFIRST\nLAST\n\n[stderr]\nERROR\n"
        # Preview events must not grow persisted conversation/tree history.
        assert all(
            call.args[0]["kind"] != "CommandOutput" for call in runtime.tree.consume.call_args_list
        )

    asyncio.run(run())


def test_preview_sanitizes_before_clipping_and_waits_for_complete_lines():
    assert preview_text([b"token=super"], []) == ""
    assert "supersecret" not in preview_text([b"token=super", b"secret\n"], [])
    assert "secret body" not in preview_text([b'password="first\nsecret body\n'], [])
    assert "private body" not in preview_text([b"-----BEGIN PRIVATE KEY-----\nprivate body\n"], [])
    output = preview_text([b"\x1b[31m[bold]literal[/bold]\x1b[0m\n"], [])
    assert "\x1b" not in output
    assert "[bold]literal[/bold]" in output
    assert "界" in preview_text(["界\n".encode()[:1], "界\n".encode()[1:]], [])
    assert len(preview_text([b"x\n" * 100000], [])) <= 131072


def test_timeout_clears_preview_when_result_is_presented(tmp_path):
    runtime = runtime_for(tmp_path, "import time; print('READY'); time.sleep(20)", timeout=0.4)

    async def run():
        app = PreviewApp(runtime=runtime)
        saw_preview = False
        async for event in runtime.stream("run"):
            if isinstance(event, CommandOutput):
                saw_preview = True
                app.present_events((event,))
                assert app.activity.command_outputs
            if isinstance(event, ToolSummary):
                app.present_events((event,))
                assert not app.activity.command_outputs
                assert event.failed
        assert saw_preview

    asyncio.run(run())


def test_shell_run_instances_preserve_streaming_and_isolate_cwd(tmp_path):
    async def run():
        shell = StreamingShell(cwd=tmp_path)
        base = shell.get_toolset()
        first = await base.for_run(None)
        second = await base.for_run(None)
        assert type(first) is type(base)
        first._cwd = Path("/")
        assert second._cwd == tmp_path

    asyncio.run(run())


def test_cancelling_stream_terminates_command(tmp_path):
    marker = tmp_path / "escaped"
    runtime = runtime_for(
        tmp_path,
        "import time, pathlib; print('READY'); time.sleep(1); "
        f"pathlib.Path({str(marker)!r}).touch(); time.sleep(20)",
    )

    async def run():
        async def consume():
            async for event in runtime.stream("run"):
                if isinstance(event, CommandOutput):
                    ready.set()

        ready = asyncio.Event()
        task = asyncio.create_task(consume())
        await asyncio.wait_for(ready.wait(), 4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(1.2)
        assert not marker.exists()

    asyncio.run(run())


def test_parallel_commands_keep_their_own_call_identity(tmp_path):
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                i: DeltaToolCall(
                    name="run_command",
                    json_args=json.dumps(
                        {
                            "command": command(f"import time; print('{name}'); time.sleep(.4)"),
                        }
                    ),
                )
                for i, name in enumerate(("ALPHA", "BETA"))
            }
        else:
            yield "Done."

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        events = [event async for event in runtime.stream("run both")]
        previews = [event for event in events if isinstance(event, CommandOutput)]
        results = {event.call_id: event for event in events if isinstance(event, ToolSummary)}
        assert len({event.call_id for event in previews}) == 2
        for event in previews:
            result = results[event.call_id]
            assert event.command == result.command
            assert event.output.strip() == result.result.strip()

    asyncio.run(run())
