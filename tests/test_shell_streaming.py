import asyncio
import json
import os
import shlex
import signal
import sys
from unittest.mock import Mock

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness.shell import Shell

from pcode.agent import create_coder
from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.runtime import CommandOutput, ToolSummary
from pcode.shell import preview_text


def command(source):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def runtime_for(workspace, source, timeout=5, mode="foreground", session=None):
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                0: DeltaToolCall(
                    name="shell",
                    json_args=json.dumps(
                        {"command": command(source), "timeout": timeout, "mode": mode}
                    ),
                )
            }
        else:
            assert calls == 2  # Output events do not add model calls.
            yield "Done."

    return AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(workspace)]), session
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
        async with asyncio.timeout(10):
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
        assert result.result.startswith("FIRST\nERROR\nLAST\nPID: ")
        assert json.loads(result.result.splitlines()[-1])["exit_code"] == 0
        assert result.process_id.isdecimal()
        assert all(
            call.args[0]["kind"] != "CommandOutput" for call in runtime.tree.consume.call_args_list
        )

    asyncio.run(run())


def test_preview_sanitizes_before_clipping_and_waits_for_complete_lines():
    assert preview_text("token=super") == ""
    assert "supersecret" not in preview_text("token=supersecret\n")
    assert "secret body" not in preview_text('password="first\nsecret body\n')
    assert "private body" not in preview_text("-----BEGIN PRIVATE KEY-----\nprivate body\n")
    output = preview_text("\x1b[31m[bold]literal[/bold]\x1b[0m\n")
    assert "\x1b" not in output
    assert "[bold]literal[/bold]" in output
    assert "界" in preview_text("界\n")
    assert len(preview_text("x\n" * 100000)) <= 131072
    assert preview_text("last line", final=True) == "last line"
    assert "supersecret" not in preview_text("token=supersecret", final=True)


@pytest.mark.parametrize("mode", ["foreground", "background"])
def test_wait_limit_returns_live_process_and_clears_preview(tmp_path, mode):
    runtime = runtime_for(
        tmp_path, "import time; print('READY'); time.sleep(60)", timeout=0.5, mode=mode
    )

    async def run():
        app = PreviewApp(runtime=runtime)
        saw_preview = False
        pid = None
        try:
            async for event in runtime.stream("run"):
                if isinstance(event, CommandOutput) and event.output:
                    saw_preview = True
                    app.present_events((event,))
                    assert app.activity.command_outputs
                if isinstance(event, ToolSummary):
                    pid = int(event.process_id)
                    app.present_events((event,))
                    assert not app.activity.command_outputs
                    assert not event.failed
                    assert "Running in background" in event.detail
                    if event.result.splitlines()[-1].startswith("{"):
                        assert json.loads(event.result.splitlines()[-1])["exit_code"] is None
                    else:
                        # Background can return before the supervisor writes status.json.
                        assert event.result.splitlines()[-1].startswith("Status: ")
                    os.kill(pid, 0)  # Foreground timeout is not process termination.
            assert saw_preview is (mode == "foreground")
        finally:
            if pid is not None:
                os.killpg(pid, signal.SIGTERM)

    asyncio.run(run())


def test_coder_uses_unmodified_upstream_shell_and_isolates_run_state(tmp_path):
    async def run():
        shell = next(c for c in create_coder(tmp_path).capabilities if isinstance(c, Shell))
        assert type(shell) is Shell
        assert shell.tools == ["shell"]
        assert shell.default_timeout == 270
        base = shell.get_toolset()
        first = await base.for_run(None)
        second = await base.for_run(None)
        assert type(first) is type(base)
        assert first is not second
        assert set(first.tools) == {"shell"}

    asyncio.run(run())


def test_cancelling_stream_terminates_command(tmp_path):
    runtime = runtime_for(
        tmp_path, "import os, time; print(f'CHILD_PID={os.getpid()}'); time.sleep(60)"
    )

    async def run():
        pid = None
        ready = asyncio.Event()

        async def consume():
            nonlocal pid
            async for event in runtime.stream("run"):
                if isinstance(event, CommandOutput) and "CHILD_PID=" in event.output:
                    pid = int(event.output.split("CHILD_PID=")[1].splitlines()[0])
                    ready.set()

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), 10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert pid is not None
        async with asyncio.timeout(10):
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.02)

    asyncio.run(run())


def test_parallel_commands_keep_their_own_call_identity(tmp_path):
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                i: DeltaToolCall(
                    name="shell",
                    tool_call_id=f"shell-{i}",
                    json_args=json.dumps(
                        {"command": command(f"import time; print('{name}'); time.sleep(.2)")}
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
        assert {event.call_id for event in previews} == {"shell-0", "shell-1"}
        for event in previews:
            result = results[event.call_id]
            assert event.command == result.command
            assert result.result.startswith(event.output.strip())
        assert calls == 2

    asyncio.run(run())


def test_preview_cap_and_nonzero_status_come_from_upstream_events(tmp_path):
    runtime = runtime_for(tmp_path, "import sys; print('x' * 20000); sys.exit(7)")

    async def run():
        events = [event async for event in runtime.stream("run")]
        result = next(e for e in events if isinstance(e, ToolSummary))
        assert result.failed and "exit 7" in result.detail
        assert "preview capped" in result.detail
        assert '"exit_code": 7' in result.error
        previews = [e for e in events if isinstance(e, CommandOutput)]
        assert "Live preview capped" in previews[-1].output
        assert len(previews[-1].output) < 17000

    asyncio.run(run())


def test_final_status_supersedes_earlier_running_event(tmp_path, monkeypatch):
    import time

    from pydantic_ai_harness.shell import _persistent

    gate = tmp_path / "exit-now"
    original = _persistent._count_lines

    def finish_during_line_count(path):
        # This happens after the event's status snapshot, before the tool return.
        gate.touch()
        deadline = time.monotonic() + 10
        status = path.with_name("status.json")
        while time.monotonic() < deadline:
            if status.exists() and json.loads(status.read_text())["exit_code"] is not None:
                return original(path)
            time.sleep(0.01)
        raise AssertionError("supervisor did not publish the exit")

    monkeypatch.setattr(_persistent, "_count_lines", finish_during_line_count)
    runtime = runtime_for(
        tmp_path,
        "import pathlib, time, sys; print('READY')\n"
        f"while not pathlib.Path({str(gate)!r}).exists(): time.sleep(.01)\n"
        "sys.exit(9)",
        timeout=0.3,
    )

    async def run():
        events = [event async for event in runtime.stream("run")]
        result = next(e for e in events if isinstance(e, ToolSummary))
        assert result.failed
        assert "exit 9" in result.detail
        assert "Running in background" not in result.detail
        assert '"exit_code": 9' in result.error

    asyncio.run(run())


def test_truncated_secret_output_is_omitted_before_inspection_and_persistence(tmp_path):
    from pcode.sessions import SavedSession

    # Synthetic content, generated without putting its body in the command text.
    marker = "SYNTHETIC_PRIVATE_BODY"
    source = (
        "import sys; print('-----BEGIN PRIVATE KEY-----'); "
        f"print(''.join(map(chr, {list(map(ord, marker))!r})) * 1000); "
        "print('-----END PRIVATE KEY-----'); sys.exit(3)"
    )
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    runtime = runtime_for(tmp_path, source, session=saved)

    async def run():
        try:
            events = [event async for event in runtime.stream("run")]
            result = next(e for e in events if isinstance(e, ToolSummary))
            assert result.failed
            assert "Output tail omitted" in result.result
            assert marker not in result.result and marker not in result.error
            assert all(marker not in e.output for e in events if isinstance(e, CommandOutput))
            assert marker not in (saved.directory / "transcript.jsonl").read_text()
        finally:
            runtime.close()

    asyncio.run(run())
