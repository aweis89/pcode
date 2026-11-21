"""Thinking is a saved, visibility-filtered part of normal scrollback."""

import asyncio
from io import StringIO

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.preferences import SETTINGS, load_preferences, save_preferences
from pcode.ui import Activity, TerminalOutput, Transcript, create_prompt


def rendered(transcript):
    buffer = StringIO()
    console = Console(file=buffer, width=60)
    with console.use_theme(transcript.rich_theme):
        for objects, end, soft_wrap in transcript.replay():
            console.print(*objects, end=end, soft_wrap=soft_wrap)
    return "\n".join(line.rstrip() for line in buffer.getvalue().split("\n"))


def test_thinking_retained_without_old_tail_limit_and_hidden_by_default():
    transcript = Transcript(Console(file=StringIO()), activity=Activity())
    text = "FIRST_THOUGHT\n" + "reasoning line\n" * 2000 + "LAST_THOUGHT\n"
    transcript.thinking(text)
    assert rendered(transcript) == ""
    transcript.activity.thinking_mode = "scrollback"
    assert rendered(transcript) == text + "\n"
    transcript.activity.thinking_mode = "off"
    assert rendered(transcript) == ""
    assert len(transcript.log.entries) == 1


def test_thinking_is_muted_and_sanitizes_controls():
    transcript = Transcript(Console(file=StringIO()), activity=Activity(thinking_mode="scrollback"))
    transcript.thinking("hello\n\x1b[31mworld\x1b[0m\r\x00\x1b]0;title\x07\n")
    replay = transcript.replay()
    assert replay[0][0][0].style == "pcode.thinking"
    text = rendered(transcript)
    assert "hello\nworld" in text
    assert "title" not in text
    assert not any(char in text for char in ("\x1b", "\r", "\x00"))


def test_show_thinking_preference(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert SETTINGS["show_thinking"].default == "activity-panel"
    assert PreviewApp().activity.thinking_mode == "activity-panel"
    assert not PreviewApp().activity.show_thinking  # Scrollback is opt-in.
    save_preferences(show_thinking="scrollback")
    assert load_preferences()["show_thinking"] == "scrollback"
    assert PreviewApp().activity.show_thinking


def test_show_thinking_command_redraws_and_saves_default(tmp_path, monkeypatch):
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from pcode.commands import SlashCompleter

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    output = StringIO()
    app = PreviewApp(console=Console(file=output))
    redraws = []
    monkeypatch.setattr(
        app.transcript, "regenerate", lambda: redraws.append(rendered(app.transcript))
    )
    app.activity.busy = True
    app.transcript.thinking("RETAINED_REASONING\n")
    assert app.registry.dispatch("/show-thinking scrollback")
    assert "RETAINED_REASONING" in redraws[-1]
    assert load_preferences()["show_thinking"] == "scrollback"
    assert app.registry.dispatch("/show-thinking")  # Bare cycles, wrapping to off.
    assert len(redraws) == 2
    assert "RETAINED_REASONING" not in redraws[-1]
    assert load_preferences()["show_thinking"] == "off"
    # Off and activity-panel both keep it out of scrollback: nothing to redraw.
    assert app.registry.dispatch("/show-thinking")
    assert load_preferences()["show_thinking"] == "activity-panel"
    assert len(redraws) == 2
    assert app.registry.dispatch("/show-thinking scrollback")
    assert app.registry.dispatch("/show-thinking off")
    assert "RETAINED_REASONING" not in redraws[-1]
    assert load_preferences()["show_thinking"] == "off"
    assert "RETAINED_REASONING" in repr(app.transcript.log.entries)
    with pytest.raises(ValueError, match="Usage"):
        app.registry.dispatch("/show-thinking invalid")
    completions = SlashCompleter(app.registry).get_completions(
        Document("/show-thinking "), CompleteEvent()
    )
    assert [item.text for item in completions] == ["off", "activity-panel", "scrollback"]


def test_tasks_heading_never_contains_thinking_and_legacy_settings_are_ignored():
    save_preferences(show_thinking="scrollback", thinking_display="expanded", thinking_lines="5")
    app = PreviewApp()
    app.activity.plan = [{"content": "Investigate", "status": "in_progress"}]
    app.transcript.thinking("**Inspecting workspace**\n")
    assert app.activity.panel_heading() == app.activity.panel_title()
    assert "Inspecting workspace" not in app.activity.panel_heading()
    assert "thinking_display" not in load_preferences()
    assert "thinking_lines" not in load_preferences()


def test_streaming_lines_are_retained_once_and_keep_answer_order():
    async def run():
        buffer = StringIO()
        app = PreviewApp(console=Console(file=buffer))
        app.activity.thinking_mode = "scrollback"
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                app.registry, activity=app.activity, input=pipe, output=DummyOutput()
            )
            writer = TerminalOutput(app.transcript.console, prompt.app)
            app.transcript.output = writer
            writer.begin_turn("Question")
            writer.thinking_delta("First ")
            writer.thinking_delta("line\nSecond line\n\nPartial")
            await writer.flush()
            assert "First line\nSecond line" in "\n".join(
                line.rstrip() for line in buffer.getvalue().splitlines()
            )
            assert "Partial" not in buffer.getvalue()
            writer.finish_thinking("First line\nSecond line\nPartial")
            writer.delta("Answer\n\n")
            writer.finish("Answer\n\n")
            writer.end_turn()
            await writer.flush()
            text = buffer.getvalue()
            assert text.count("First line") == 1
            assert text.count("Second line") == 1
            assert text.count("Partial") == 1
            assert text.index("Partial") < text.index("Answer")
            assert not writer._thinking_tail
            shown = rendered(app.transcript)
            assert shown.count("First line") == 1
            app.activity.thinking_mode = "off"
            hidden = rendered(app.transcript)
            assert "First line" not in hidden
            assert "Answer" in hidden
            assert "Question" in hidden

    asyncio.run(run())


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("mode", ["palette", "terminal"])
@pytest.mark.parametrize(
    "source",
    [
        "Readable thinking",
        "**Bold** and *italic*",
        "# Heading",
        "- item",
        "```python\nprint(1)\n```",
    ],
)
def test_thinking_style_is_dim_and_theme_aware(theme, mode, source):
    console = Console(file=StringIO(), force_terminal=True)
    transcript = Transcript(
        console,
        theme=theme,
        activity=Activity(thinking_mode="scrollback"),
        preferences={"syntax_dark": "gruvbox-dark", "syntax_light": "gruvbox-light"}
        if mode == "palette"
        else {"syntax_dark": "terminal", "syntax_light": "terminal"},
    )
    assert transcript.terminal_colors == (mode == "terminal")
    transcript.thinking(source)
    with console.use_theme(transcript.rich_theme):
        segments = list(console.render(transcript.replay()[0][0][0]))
    visible = [segment for segment in segments if segment.text.strip()]
    assert visible
    for segment in visible:
        assert segment.style.dim
        if mode == "palette":
            assert segment.style.color.triplet.hex == transcript.palette.muted
    if source.startswith("**"):
        assert next(segment for segment in visible if "Bold" in segment.text).style.bold


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("**Inspecting workspace**", "Inspecting workspace\n\n"),
        ("**Heading**\n\nBody with **inline bold**.\n", "Heading\n\nBody with inline bold.\n\n"),
        ("**First**\n**Second**\n", "First\nSecond\n\n"),
        ("**Unclosed", "**Unclosed\n\n"),
        ("Unopened**", "Unopened**\n\n"),
        ("Plain **inline** text", "Plain inline text\n\n"),
        ("*Single* and `code`", "Single and code\n\n"),
        ("", ""),
    ],
)
def test_thinking_renders_muted_markdown_only_for_display(source, expected):
    buffer = StringIO()
    transcript = Transcript(
        Console(file=buffer, width=60), activity=Activity(thinking_mode="scrollback")
    )
    transcript.thinking(source)
    assert rendered(transcript) == expected
    assert buffer.getvalue() == render_raw(transcript)
    assert transcript.log.entries[0].args == (source,)


def render_raw(transcript):
    buffer = StringIO()
    console = Console(file=buffer, width=60)
    with console.use_theme(transcript.rich_theme):
        for objects, end, soft_wrap in transcript.replay():
            console.print(*objects, end=end, soft_wrap=soft_wrap)
    return buffer.getvalue()


def test_streamed_thinking_renders_split_markdown_and_buffers_code_and_lists():
    buffer = StringIO()
    transcript = Transcript(
        Console(file=buffer, width=60), activity=Activity(thinking_mode="scrollback")
    )
    with create_pipe_input() as pipe:
        app = PreviewApp()
        prompt = create_prompt(app.registry, input=pipe, output=DummyOutput())
        writer = TerminalOutput(transcript.console, prompt.app)
        writer.commit_thinking = transcript.thinking
        for chunk in ["*", "*First", "*", "*\n\n*", "*Second*", "*\n\n"]:
            writer.thinking_delta(chunk)
        assert rendered(transcript) == "First\n\nSecond\n\n"
        writer.thinking_delta("```python\nprint('hello')\n")
        assert "hello" not in buffer.getvalue()
        writer.thinking_delta("```\n")
        assert "hello" in buffer.getvalue()
        writer.thinking_delta("- one\n\n- two\n")
        assert "one" not in buffer.getvalue()
        writer.finish_thinking("ignored streamed fallback", complete=True)
        assert "one" in buffer.getvalue()
        assert "two" in buffer.getvalue()
        assert "ignored streamed fallback" not in buffer.getvalue()
        assert buffer.getvalue() == render_raw(transcript)
        assert "First\n\nSecond\n\n" in rendered(transcript)
        writer.finish_thinking("**Fallback**")
        assert rendered(transcript).endswith("Fallback\n\n")


def press(session, key):
    from prompt_toolkit.key_binding.key_processor import KeyPress

    session.app.key_processor.feed(KeyPress(key))
    session.app.key_processor.process_keys()


def test_ctrl_t_selects_modes_and_redraws_only_when_scrollback_flips(tmp_path, monkeypatch):
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.keys import Keys

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = PreviewApp(console=Console(file=StringIO()))
    redraws = []
    monkeypatch.setattr(app.transcript, "regenerate", lambda: redraws.append(1))

    async def run():
        with create_pipe_input() as pipe:
            session = create_prompt(
                app.registry,
                activity=app.activity,
                on_thinking=app.show_thinking,
                input=pipe,
                output=DummyOutput(),
            )
            with set_app(session.app):
                session.default_buffer.text = "retained draft"
                for key, mode, redraw_count in [
                    ("b", "scrollback", 1),
                    ("b", "scrollback", 1),
                    ("o", "off", 2),
                    ("a", "activity-panel", 2),
                    ("b", "scrollback", 3),
                ]:
                    before = (app.activity.thinking_mode, len(redraws))
                    press(session, Keys.ControlT)
                    assert (app.activity.thinking_mode, len(redraws)) == before
                    assert session.shortcuts.help_title == "Thinking visibility"
                    press(session, key)
                    assert app.activity.thinking_mode == mode
                    assert len(redraws) == redraw_count
                    assert load_preferences()["show_thinking"] == mode
                    assert not session.shortcuts.visible
                    assert session.default_buffer.text == "retained draft"

    asyncio.run(run())


def test_ctrl_t_without_an_app_still_selects_the_activity_mode():
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.keys import Keys

    activity = Activity()

    async def run():
        with create_pipe_input() as pipe:
            registry = PreviewApp(console=Console(file=StringIO())).registry
            session = create_prompt(registry, activity=activity, input=pipe, output=DummyOutput())
            with set_app(session.app):
                for key, mode in [("o", "off"), ("b", "scrollback"), ("a", "activity-panel")]:
                    before = activity.thinking_mode
                    press(session, Keys.ControlT)
                    assert activity.thinking_mode == before
                    press(session, key)
                    assert activity.thinking_mode == mode
                    assert not session.shortcuts.visible

    asyncio.run(run())


@pytest.mark.parametrize(
    "text, shown",
    [
        ("**Tracing the resize path**\n\nI need to check", "Tracing the resize path"),
        ("## Plan\n```py\n# a comment, not a title\n```\nmore", "Plan"),
        ("**A** and **B** are emphasis", "A and B are emphasis"),
        ("__init__", "__init__"),
        ("first\nnewest line", "newest line"),
        ("", ""),
    ],
)
def test_thinking_row_prefers_the_newest_section_title(text, shown):
    from pcode.ui import latest_thought

    assert latest_thought(text) == shown


def test_head_rows_keeps_the_first_rows_and_marks_the_cut():
    from rich.cells import cell_len

    from pcode.ui import head_rows

    assert head_rows("short", 10, 3) == ["short"]
    assert head_rows("", 10, 3) == []
    assert head_rows("text", 0, 3) == [] and head_rows("text", 10, 0) == []
    words = " ".join(f"w{i:02d}" for i in range(20))  # Wraps to 4 rows of 19 cells.
    kept = head_rows(words, 20, 3)
    assert kept == ["w00 w01 w02 w03 w04", "w05 w06 w07 w08 w09", "w10 w11 w12 w13 w14…"]
    assert all(cell_len(row) <= 20 for row in kept)
    # A full row gives up a cell for the ellipsis rather than overflowing.
    assert head_rows("x" * 50, 10, 2) == ["x" * 10, "x" * 9 + "…"]
    # A wide character cannot be split: it is dropped whole to make room.
    wide = head_rows("漢" * 10, 6, 1)
    assert cell_len(wide[0]) <= 6 and wide[0].endswith("…")


def test_a_background_workers_calls_do_not_split_a_streaming_thought():
    from pcode.runtime import Thinking, ThinkingDelta, ToolStarted, ToolSummary
    from pcode.stream_display import present_events, present_stream_event

    activity = Activity(thinking_mode="scrollback")
    transcript = Transcript(Console(file=StringIO(), width=60), activity=activity)
    with create_pipe_input() as pipe:
        app = PreviewApp()
        prompt = create_prompt(app.registry, input=pipe, output=DummyOutput())
        writer = TerminalOutput(transcript.console, prompt.app)
        writer.commit_thinking = transcript.thinking

        def feed(event):
            present_stream_event(
                event,
                output=writer,
                transcript=transcript,
                activity=activity,
                present=lambda events: present_events(
                    events, activity=activity, transcript=transcript, edits=[]
                ),
            )

        delegate = ToolStarted("delegate_task", "worker · review", "d1", agent="worker")
        feed(delegate)
        feed(ThinkingDelta("I"))
        # The worker reports while the parent is still mid-word.
        feed(ToolStarted("delegate_task", "worker · review", "d1", activity="shell"))
        feed(ToolStarted("shell", "git show", "d1:c1", parent_call_id="d1"))
        feed(ToolSummary("shell", "git show", call_id="d1:c1", parent_call_id="d1"))
        feed(ThinkingDelta("'m checking"))
        feed(Thinking("I'm checking"))
        assert rendered(transcript) == "I'm checking\n\n"
        # A call that settles into scrollback still commits the thought above it.
        feed(ThinkingDelta("Next"))
        feed(ToolSummary("read_file", "a.py", call_id="t1"))
        assert rendered(transcript).startswith("I'm checking\n\nNext\n\n")


def test_replay_does_not_split_a_thought_at_a_sub_agent_call():
    from types import SimpleNamespace

    from pcode.sessions import SavedSession

    child = {"kind": "ToolSummary", "name": "shell", "parent_call_id": "d1"}
    records = [
        {"kind": "ThinkingDelta", "text": "I"},
        child,
        {"kind": "ThinkingDelta", "text": "'m checking"},
        {"kind": "Thinking", "text": "I'm checking"},
        {"kind": "Message", "markdown": "Answer"},
    ]
    saved = SimpleNamespace(active_records=lambda end=None: iter(records))
    assert list(SavedSession.transcript_records(saved)) == [
        child,
        {"kind": "thinking_partial", "text": "I'm checking"},
        {"kind": "Message", "markdown": "Answer"},
    ]


def test_a_scrollback_write_mid_thought_does_not_repeat_the_thought():
    from pcode.runtime import Thinking, ThinkingDelta, ToolSummary
    from pcode.stream_display import present_events, present_stream_event

    activity = Activity(thinking_mode="scrollback")
    transcript = Transcript(Console(file=StringIO(), width=60), activity=activity)
    with create_pipe_input() as pipe:
        app = PreviewApp()
        prompt = create_prompt(app.registry, input=pipe, output=DummyOutput())
        writer = TerminalOutput(transcript.console, prompt.app)
        writer.commit_thinking = transcript.thinking

        def feed(event):
            present_stream_event(
                event,
                output=writer,
                transcript=transcript,
                activity=activity,
                present=lambda events: present_events(
                    events, activity=activity, transcript=transcript, edits=[]
                ),
            )

        feed(ThinkingDelta("Checking"))
        # A background job's result lands before the thought's completion.
        feed(ToolSummary("shell", "make test", call_id="t1"))
        feed(Thinking("Checking"))
        assert rendered(transcript).count("Checking") == 1
        # The next thought, arriving whole, is still written.
        feed(Thinking("Whole"))
        assert "Whole" in rendered(transcript)


def test_a_turn_ended_mid_thought_lets_the_next_whole_thought_through():
    transcript = Transcript(
        Console(file=StringIO(), width=60), activity=Activity(thinking_mode="scrollback")
    )
    with create_pipe_input() as pipe:
        app = PreviewApp()
        prompt = create_prompt(app.registry, input=pipe, output=DummyOutput())
        writer = TerminalOutput(transcript.console, prompt.app)
        writer.commit_thinking = transcript.thinking
        writer.thinking_delta("Cancelled")
        writer.end_turn()
        writer.finish_thinking("Whole", complete=True)
        assert "Whole" in rendered(transcript)
