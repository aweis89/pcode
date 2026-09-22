"""Resume replaces history and uses the ordinary bounded redraw projection."""

import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from rich.console import Console

from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.preferences import SETTINGS, save_preferences
from pcode.runtime import Message, ToolSummary
from pcode.sessions import SavedSession
from pcode.ui import TerminalOutput, Transcript


def rendered(transcript):
    stream = StringIO()
    console = Console(file=stream, width=100, color_system=None)
    with console.use_theme(transcript.rich_theme):
        for objects, end, _ in transcript.replay():
            console.print(*objects, end=end)
    return stream.getvalue()


@pytest.fixture
def saved(tmp_path):
    session = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        yield session
    finally:
        session.close()


def test_resume_redraws_more_than_40_records_and_restores_hidden_payloads(saved):
    saved.append("turn_started", prompt="FIRST_PROMPT")
    saved.append("steering", prompt="STEERING_PROMPT")
    saved.append("ThinkingDelta", text="HIDDEN_THOUGHT")
    saved.append("Thinking", text="HIDDEN_THOUGHT")
    saved.event(ToolSummary("run_command", "run", command="echo result", result="HIDDEN_RESULT"))
    for index in range(80):
        saved.event(Message(f"ANSWER_{index:03d}"))
    saved.append("turn_completed")
    app = PreviewApp(
        model="test:local",
        runtime=SimpleNamespace(session=saved),
        console=Console(file=StringIO(), force_terminal=True),
    )
    app.activity.show_thinking = False
    app.transcript.command_scrollback = False
    app.transcript.print("OLD_SESSION")
    output = Mock()
    app.transcript.output = output
    app.replay()
    output.regenerate.assert_called_once_with(app.transcript.replay)
    output.print.assert_not_called()  # No intermediate rendering before the atomic redraw.
    text = rendered(app.transcript)
    assert "OLD_SESSION" not in text
    assert text.index("FIRST_PROMPT") < text.index("STEERING_PROMPT") < text.index("ANSWER_000")
    assert text.count("ANSWER_000") == text.count("ANSWER_079") == 1
    assert "HIDDEN_THOUGHT" not in text and "HIDDEN_RESULT" not in text
    app.activity.show_thinking = True
    app.transcript.command_scrollback = True
    shown = rendered(app.transcript)
    assert shown.count("HIDDEN_THOUGHT") == shown.count("HIDDEN_RESULT") == 1
    assert shown.index("HIDDEN_THOUGHT") < shown.index("HIDDEN_RESULT") < shown.index("ANSWER_000")
    count = len(app.transcript.log.entries)
    assert rendered(app.transcript) == shown
    app.replay()
    assert rendered(app.transcript) == shown
    assert len(app.transcript.log.entries) == count
    assert app.activity.tools.calls == []


def test_configured_budget_matches_live_retention_and_resume(saved):
    save_preferences(transcript_max_chars="1000")
    live = Transcript(Console(file=StringIO()))
    output = TerminalOutput(live.console, Mock())
    live.output = output
    for index in range(8):
        event = Message(f"ANSWER_{index}: " + "x" * 300)
        output.delta(event.markdown)
        output.finish(event.markdown)
        saved.event(event)
    app = PreviewApp(
        model="test:local",
        runtime=SimpleNamespace(session=saved),
        console=Console(file=StringIO()),
    )
    app.replay()
    assert app.transcript.log.max_chars == live.log.max_chars == 1000
    assert app.transcript.log.limit == live.log.limit == 10
    assert app.transcript.log.dropped and live.log.dropped
    assert app.transcript.log.chars <= 1000
    assert rendered(app.transcript) == rendered(live)
    assert "ANSWER_0" not in rendered(live) and "ANSWER_7" in rendered(live)
    # Redirected resume prints once, with no erase escapes or redraw duplicates.
    text = app.transcript.console.file.getvalue()
    assert text.count("ANSWER_7") == 1
    assert "ANSWER_0" not in text and "\x1b" not in text
    app.transcript.regenerate()
    assert app.transcript.console.file.getvalue() == text


@pytest.mark.parametrize("budget", [1, 100, 1000])
def test_live_and_resumed_oversized_answer_survive_trailing_separator(saved, budget):
    save_preferences(transcript_max_chars=str(budget))
    live = Transcript(Console(file=StringIO()))
    output = TerminalOutput(live.console, Mock())
    live.output = output
    output.begin_turn("PROMPT")
    assert "PROMPT" in rendered(live)
    answer = "NEWEST" * 1000
    output.delta(answer)
    output.finish(answer)
    saved.event(Message(answer))
    app = PreviewApp(
        model="test:local",
        runtime=SimpleNamespace(session=saved),
        console=Console(file=StringIO()),
    )
    app.replay()
    assert len(live.log.entries) == len(app.transcript.log.entries) == 1
    assert live.log.chars == app.transcript.log.chars == len(answer)
    assert rendered(live) == rendered(app.transcript)
    assert "NEWEST" in rendered(live)


@pytest.mark.parametrize("streamed", [False, True])
def test_resume_preserves_separate_thinking_block_boundaries(saved, streamed):
    for text in ("FIRST", "SECOND"):
        if streamed:
            saved.append("ThinkingDelta", text=text)
        saved.append("Thinking", text=text)
    app = PreviewApp(
        model="test:local",
        runtime=SimpleNamespace(session=saved),
        console=Console(file=StringIO()),
    )
    app.activity.show_thinking = True
    app.replay()
    assert app.transcript.log.entries[-1].args == ("FIRST\n\nSECOND\n\n",)
    assert "FIRSTSECOND" not in rendered(app.transcript)


def test_restore_retains_only_newest_oversized_entry_before_rendering():
    transcript = Transcript(
        Console(file=StringIO(), force_terminal=True),
        preferences={"transcript_max_chars": "1000"},
    )
    transcript.output = Mock()
    with transcript.restore():
        for _ in range(30):
            transcript.events((Message("discarded" * 100),))
        transcript.events((Message("NEWEST" * 1000),))
    transcript.output.print.assert_not_called()
    transcript.output.regenerate.assert_called_once_with(transcript.replay)
    assert len(transcript.log.entries) == 1
    assert transcript.log.chars == 6000
    assert "discarded" not in rendered(transcript)
    assert "NEWEST" in rendered(transcript)


def test_failed_restore_preserves_previous_history():
    transcript = Transcript(Console(file=StringIO()), preferences={})
    transcript.events((Message("previous"),))
    with pytest.raises(ValueError):
        with transcript.restore():
            transcript.events((Message("incomplete"),))
            raise ValueError("bad journal")
    assert "previous" in rendered(transcript)
    assert "incomplete" not in rendered(transcript)
    assert not transcript.log.capture_only


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "unlimited"])
def test_transcript_budget_requires_positive_integer(value):
    with pytest.raises(ValueError, match="positive integer"):
        SETTINGS["transcript_max_chars"].validate("transcript_max_chars", value)


def test_transcript_budget_default_and_scaled_entry_guard():
    transcript = Transcript(Console(file=StringIO()), preferences={})
    assert SETTINGS["transcript_max_chars"].default == "2000000"
    assert transcript.log.max_chars == 2_000_000
    assert transcript.log.limit == 20_000
    tiny = Transcript(Console(file=StringIO()), preferences={"transcript_max_chars": "1"})
    assert tiny.log.limit == 1
    tiny.user("newest oversized prompt")
    assert "newest oversized prompt" in rendered(tiny)


@pytest.mark.parametrize("save", [False, True])
def test_tree_navigation_replaces_abandoned_output_and_can_clear_root(tmp_path, save):
    async def run():
        async def model(messages, info):
            yield "Answer"

        saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions") if save else None
        runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)), saved)
        app = PreviewApp(model="test:local", runtime=runtime, console=Console(file=StringIO()))
        try:
            _ = [event async for event in runtime.stream("ANCESTOR")]
            ancestor = runtime.tree.active
            _ = [event async for event in runtime.stream("ABANDONED")]
            app.transcript.user("ABANDONED")
            await app.navigate_tree(ancestor)
            assert rendered(app.transcript).count("ANCESTOR") == 1
            assert "ABANDONED" not in rendered(app.transcript)
            await app.navigate_tree(ancestor)
            assert rendered(app.transcript).count("ANCESTOR") == 1
            await app.navigate_tree(None)
            assert "ANCESTOR" not in rendered(app.transcript)
        finally:
            runtime.close()

    asyncio.run(run())


def test_incomplete_thinking_does_not_buffer_interleaved_history():
    consumed = []

    def records():
        yield {"kind": "ThinkingDelta", "text": "thought"}
        for index in range(1000):
            consumed.append(index)
            yield {"kind": "ToolSummary", "name": "read_file", "detail": str(index)}

    iterator = SavedSession.transcript_records(SimpleNamespace(active_records=records))
    assert next(iterator) == {"kind": "thinking_partial", "text": "thought"}
    assert len(consumed) == 1
    assert next(iterator)["kind"] == "ToolSummary"
    assert len(consumed) == 1
    assert next(iterator)["detail"] == "1"
