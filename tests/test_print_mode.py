"""A command-line prompt runs first in the editor, or alone with --print."""

import asyncio
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.runtime import (
    EditCompleted,
    Message,
    RunStatus,
    TextDelta,
    Thinking,
    ThinkingDelta,
    ToolStarted,
    ToolSummary,
)


def test_initial_prompt_is_sent_before_any_typing():
    async def run():
        calls = []
        answered = asyncio.Event()

        class Runtime:
            session = None
            recovery_blocked = ""

            async def stream(self, text):
                calls.append(text)
                yield Message("answer")
                answered.set()

        app = PreviewApp(
            model="test:local",
            runtime=Runtime(),
            console=Console(file=StringIO()),
            initial_prompt="first from argv",
        )
        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            from pcode.ui import create_prompt

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    await asyncio.wait_for(answered.wait(), 5)
                    assert calls == ["first from argv"]
                    async with asyncio.timeout(5):
                        while app.activity.busy:
                            await asyncio.sleep(0.01)
                    pipe.send_text("\x04")
                    await asyncio.wait_for(task, 5)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_print_streams_reply_to_stdout_and_activity_to_transcript():
    class Runtime:
        session = None
        recovery_blocked = ""

        async def stream(self, text):
            assert text == "what changed?"
            yield RunStatus("Thinking…")
            yield ThinkingDelta("hidden reasoning")
            yield Thinking("hidden reasoning")
            yield ToolStarted("read_file", "example.py", "call-1")
            yield ToolSummary("read_file", "example.py", call_id="call-1")
            yield EditCompleted(
                "call-2", "example.py", "edit", patch="-a\n+b\n", added=1, removed=1
            )
            yield TextDelta("**Two** things")
            yield TextDelta(" changed.")
            yield Message("**Two** things changed.")
            yield Message("Final structured output")

    transcript = StringIO()
    stdout = StringIO()
    app = PreviewApp(
        model="test:local",
        runtime=Runtime(),
        console=Console(file=transcript, color_system=None, width=80),
    )
    assert asyncio.run(app.run_print_async("what changed?", stdout=stdout))
    assert stdout.getvalue() == "**Two** things changed.\n\nFinal structured output\n\n"
    printed = transcript.getvalue()
    assert "✓ Read" in printed
    assert "example.py" in printed
    assert "hidden reasoning" not in printed
    assert "Two" not in printed


def test_print_reports_failure_on_transcript_and_returns_false():
    class Runtime:
        session = None
        recovery_blocked = ""

        async def stream(self, text):
            yield TextDelta("partial")
            raise RuntimeError("private provider body")

    transcript = StringIO()
    stdout = StringIO()
    app = PreviewApp(
        model="test:local",
        runtime=Runtime(),
        console=Console(file=transcript, color_system=None, width=80),
    )
    assert not asyncio.run(app.run_print_async("go", stdout=stdout))
    assert stdout.getvalue() == "partial\n"
    printed = transcript.getvalue()
    assert "Agent failed" in printed
    assert "private provider body" not in printed


def test_print_without_a_model_uses_the_offline_preview():
    stdout = StringIO()
    app = PreviewApp(console=Console(file=StringIO(), color_system=None))
    assert asyncio.run(app.run_print_async("hello", stdout=stdout))
    assert "local UI preview" in stdout.getvalue()


@pytest.mark.parametrize("shown", [True, False])
def test_print_shows_thinking_only_when_preferred(shown):
    class Runtime:
        session = None
        recovery_blocked = ""

        async def stream(self, text):
            yield Thinking("visible reasoning")
            yield Message("done")

    transcript = StringIO()
    app = PreviewApp(
        model="test:local",
        runtime=Runtime(),
        console=Console(file=transcript, color_system=None, width=80),
    )
    app.activity.show_thinking = shown
    assert asyncio.run(app.run_print_async("go", stdout=StringIO()))
    assert ("visible reasoning" in transcript.getvalue()) is shown
