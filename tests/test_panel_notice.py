"""Short-lived acknowledgements belong to the live panel, never to scrollback."""

import asyncio
from io import StringIO

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.ui import NOTICE_ROWS, Activity, Transcript, create_prompt


def test_notice_wraps_to_the_pane_and_is_bounded():
    activity = Activity()
    assert activity.notice_rows(40) == []
    activity.flash("Show thinking: off. Usage: /show-thinking [on|off] (Ctrl+T)")
    rows = activity.notice_rows(30)
    assert [style for style, _ in rows] == ["class:activity.notice"] * len(rows)
    assert all(len(text) <= 30 for _, text in rows)
    assert "".join(text for _, text in rows).replace(" ", "").startswith("Showthinking:off.")
    activity.flash("\n".join(f"line {i}" for i in range(NOTICE_ROWS + 4)))
    assert len(activity.notice_rows(80)) == NOTICE_ROWS


def test_notice_expires_without_further_input():
    activity = Activity()
    activity.flash("Theme: light.", seconds=0.0)
    assert not activity.notice_shown
    assert activity.notice_rows(80) == []


def test_flash_without_a_live_panel_falls_back_to_a_printed_note():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None), activity=Activity())
    transcript.flash("Theme: light.")
    assert "Theme: light." in stream.getvalue()


def test_toggle_renders_above_the_editor_instead_of_entering_scrollback():
    async def run():
        stream = StringIO()
        app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
        with create_pipe_input() as pipe:
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
            session = create_prompt(
                CommandRegistry(),
                activity=app.activity,
                transcript=app.transcript,
                on_submit=lambda text: None,
                input=pipe,
                output=output,
            )
            app.transcript.output = type(
                "Stub", (), {"app": session.app, "print": lambda *a, **k: None}
            )()
            app.show_thinking("off")
            stream.seek(0)
            stream.truncate()
            with set_app(session.app):
                session.app.renderer.render(session.app, session.app.layout)
            return stream.getvalue()

    screen = asyncio.run(run())
    assert "Show thinking: off" in screen
    # The frame below it proves the notice is chrome above the editor.
    assert screen.index("Show thinking: off") < screen.index("┌")


def test_side_question_and_job_icons_line_up_with_the_turn_spinner():
    from pcode.aside import Aside

    async def run():
        stream = StringIO()
        app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
        activity = app.activity
        activity.prompt_state = "running"
        activity.asides = [Aside(question="why?")]
        activity.jobs = [("class:activity.job", "\u27f3 j1 \u00b7 serving \u00b7 3s")]
        with create_pipe_input() as pipe:
            output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=app.transcript,
                on_submit=lambda text: None,
                input=pipe,
                output=output,
            )
            with set_app(session.app):
                session.app.renderer.render(session.app, session.app.layout)
            screen = session.app.renderer._last_screen
            return [
                "".join(screen.data_buffer[row][col].char for col in range(80)).rstrip()
                for row in range(screen.height)
            ]

    lines = [line for line in asyncio.run(run()) if line.strip()]
    status = next(line for line in lines if "Working" in line)
    aside = next(line for line in lines if " btw " in line)
    job = next(line for line in lines if "j1" in line)
    columns = {len(line) - len(line.lstrip()) for line in (status, aside, job)}
    assert columns == {1}, (status, aside, job)
