"""A command-line prompt runs first in the editor, or alone with --print."""

import asyncio
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.color import Color
from rich.console import Console
from rich.text import Text

from pcode.app import PreviewApp
from pcode.jobs import Job, JobRegistry
from pcode.preferences import save_preferences
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
from pcode.ui import PALETTES


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


@pytest.mark.parametrize("failed_turn", [False, True])
def test_print_reports_background_completion_without_repeating_job_inspection(
    tmp_path, failed_turn
):
    jobs = JobRegistry()
    jobs.jobs["j14"] = Job(
        id="j14",
        command="make test",
        directory=tmp_path,
        supervisor_pid=0,
        started_at=1.0,
        ended_at=2.0,
        exit_code=2,
        background=True,
    )

    class Runtime:
        session = None
        recovery_blocked = ""

        async def stream(self, text):
            yield ToolSummary("wait_for_job", "j14 · exit 2", failed=True, outcome="success")
            yield ToolSummary("job_output", "j14 · exit 2", failed=True, outcome="success")
            assert transcript.getvalue() == ""
            if failed_turn:
                raise RuntimeError("turn failed")
            yield Message("done")

    transcript = StringIO()
    runtime = Runtime()
    runtime.jobs = jobs
    app = PreviewApp(
        model="test:local",
        runtime=runtime,
        console=Console(file=transcript, color_system=None, width=100),
    )
    assert asyncio.run(app.run_print_async("go", stdout=StringIO())) is not failed_turn
    printed = transcript.getvalue()
    assert printed.count("✗ Run(bg j14) · exit 2 · 1.0s") == 1
    assert "wait_for_job" not in printed and "job_output" not in printed


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
    # An interrupted block still reaches the reader, terminated.
    assert stdout.getvalue() == "partial\n\n"
    printed = transcript.getvalue()
    assert "Agent failed" in printed
    assert "private provider body" not in printed


class TerminalStringIO(StringIO):
    """Stand in for a terminal so Rich chooses rendering over raw source."""

    def isatty(self) -> bool:
        return True


def plain_text(stdout: TerminalStringIO) -> str:
    """Rendered output carries styling; compare the words it displays."""
    return Text.from_ansi(stdout.getvalue()).plain


def test_print_renders_markdown_on_a_terminal():
    class Runtime:
        session = None
        recovery_blocked = ""

        async def stream(self, text):
            yield TextDelta("**Two** things")
            yield TextDelta(" changed.")
            yield Message("**Two** things changed.\n\n- `example.py`\n")

    stdout = TerminalStringIO()
    app = PreviewApp(
        model="test:local",
        runtime=Runtime(),
        console=Console(file=StringIO(), color_system=None, width=80),
    )
    assert asyncio.run(app.run_print_async("what changed?", stdout=stdout))
    printed = plain_text(stdout)
    assert "Two things changed." in printed
    assert "example.py" in printed
    # Rendered once as a settled block: no markdown source, no duplicated deltas.
    assert "**" not in printed
    assert printed.count("things changed") == 1


def test_print_rendering_follows_the_resolved_light_or_dark_profile(monkeypatch):
    """The reply is a second console, so it has to inherit the transcript's palette and syntax."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")

    class Runtime:
        session = None
        recovery_blocked = ""

        async def stream(self, text):
            yield Message("# Heading\n\n```python\nimport os\n```\n")

    def rendered(theme: str) -> str:
        stdout = TerminalStringIO()
        app = PreviewApp(
            theme=theme,
            model="test:local",
            runtime=Runtime(),
            console=Console(file=StringIO(), color_system=None, width=80),
        )
        assert asyncio.run(app.run_print_async("what changed?", stdout=stdout))
        return stdout.getvalue()

    save_preferences(syntax_dark="gruvbox-dark", syntax_light="gruvbox-light")
    for theme in ("light", "dark"):
        output = rendered(theme)
        accent = ";".join(Color.parse(PALETTES[theme].accent).get_ansi_codes())
        assert accent in output, f"{theme} heading ignores the palette"
    # Fenced code follows the per-palette Pygments style, so the two differ.
    assert rendered("light") != rendered("dark")


def test_print_renders_a_partial_block_left_by_a_failure_on_a_terminal():
    class Runtime:
        session = None
        recovery_blocked = ""

        async def stream(self, text):
            yield TextDelta("**partial** answer")
            raise RuntimeError("private provider body")

    stdout = TerminalStringIO()
    app = PreviewApp(
        model="test:local",
        runtime=Runtime(),
        console=Console(file=StringIO(), color_system=None, width=80),
    )
    assert not asyncio.run(app.run_print_async("go", stdout=stdout))
    assert "partial answer" in plain_text(stdout)


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
