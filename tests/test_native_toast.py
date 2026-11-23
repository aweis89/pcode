"""Brief confirmations as the terminal's own toast (popup protocol 5, `drawer.toast`)."""

import asyncio
import base64
import re
from io import StringIO

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from pcode import drawer, preferences, theme
from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.ui import create_prompt

TOAST = re.compile(r"\x1b_pcode;toast;(\d+);(\d+);([A-Za-z0-9+/=]*)\x1b\\")


def test_toast_is_one_plain_line_that_cannot_end_the_apc():
    apc = drawer.toast("Copied\x1b\\ code\n\tblock" + "x" * 500, rows_above=2, seconds=5)
    (match,) = TOAST.findall(apc)
    rows, ms, encoded = match
    assert (rows, ms) == ("2", "5000")
    text = base64.b64decode(encoded).decode()
    assert text.startswith("Copied \\ code block")
    assert len(text) == drawer.TOAST_MAX
    assert drawer.toast("x", rows_above=-3, seconds=-1).startswith("\x1b_pcode;toast;0;0;")


def render(monkeypatch, protocol, *flashes):
    """Each notice flashed then rendered: the output written, and what the pane drew."""
    monkeypatch.setattr(theme, "_popup_protocol", protocol)
    monkeypatch.setattr(preferences, "load_preferences", lambda: {})

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
            screens = []
            with set_app(session.app):
                for text in flashes:
                    if text is not None:
                        app.activity.flash(text)
                    session.app.renderer.render(session.app, session.app.layout)
                    session.app.after_render.fire()
                    screen = session.app.renderer._last_screen
                    screens.append(
                        "\n".join(
                            "".join(screen.data_buffer[row][col].char for col in range(80))
                            for row in range(screen.height)
                        )
                    )
            return stream.getvalue(), screens

    return asyncio.run(run())


def test_a_terminal_with_toasts_shows_each_notice_once_and_not_in_the_pane(monkeypatch):
    written, screens = render(
        monkeypatch, drawer.TOAST_PROTOCOL, "Copied answer.", None, "Opened x"
    )
    toasts = [base64.b64decode(encoded).decode() for _, _, encoded in TOAST.findall(written)]
    # A redraw with the same notice sends nothing more.
    assert toasts == ["Copied answer.", "Opened x"]
    # Placed from the editor box's top, which is above the cursor's row.
    assert all(int(rows) >= 1 for rows, _, _ in TOAST.findall(written))
    assert not any("Copied answer." in screen for screen in screens)


def test_an_older_terminal_draws_the_notice_in_the_pane(monkeypatch):
    written, screens = render(monkeypatch, drawer.TOAST_PROTOCOL - 1, "Copied answer.")
    assert "pcode;toast" not in written
    assert "Copied answer." in screens[0]
