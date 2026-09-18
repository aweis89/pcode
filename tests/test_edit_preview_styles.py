import asyncio
from io import StringIO

import pytest
from prompt_toolkit.styles import Style
from pygments.token import Generic
from rich.cells import cell_len
from rich.console import Console
from rich.syntax import Syntax

from pcode.edit_transcript import edit_preview_rows
from pcode.ui import Transcript


@pytest.mark.parametrize("theme", ["monokai", "friendly", "ansi_dark", "ansi_light"])
def test_preview_colors_match_completed_diff_theme_without_its_background(theme):
    rows = edit_preview_rows("-old\n+new\n context", 40, theme)
    attrs = [Style.from_dict({}).get_attrs_for_style_str(style) for style, _ in rows]
    syntax = Syntax.get_theme(theme)
    for attr, token in zip(attrs, (Generic.Deleted, Generic.Inserted)):
        color = syntax.get_style_for_token(token).color
        if theme.startswith("ansi_"):
            # Keep terminal-native colors, not hard-coded RGB approximations.
            assert attr.color == "ansi" + color.name.replace("bright_", "bright")
        else:
            assert attr.color == color.get_truecolor().hex.lstrip("#")
        assert not attr.bgcolor
    assert not attrs[2].color
    assert attrs[0].color != attrs[1].color
    assert [text for _, text in rows] == ["-old", "+new", " context"]


def test_wrapped_continuations_keep_the_source_line_color():
    rows = edit_preview_rows("-abcd+efgh\n+ijkl-mnop", 5, "monokai")
    assert [text for _, text in rows] == ["-abcd", "+efgh", "+ijkl", "-mnop"]
    assert rows[0][0] == rows[1][0]
    assert rows[2][0] == rows[3][0]
    assert rows[0][0] != rows[2][0]
    # Tail clipping can start mid-line without losing its deletion color.
    assert rows[-3][0] == rows[0][0]


def test_preview_is_literal_sanitized_and_cell_width_aware():
    rows = edit_preview_rows(
        '+[bold]```diff\n-\x1b[31m界界界\n+token = "synthetic-secret"', 8, "monokai"
    )
    text = "\n".join(line for _, line in rows)
    assert "[bold]" in text and "```diff" in text.replace("\n", "")
    assert "\x1b" not in text and "synthetic-secret" not in text
    assert "[redacted]" in text.replace("\n", "")
    assert all(cell_len(line) <= 8 for _, line in rows)


def test_theme_and_terminal_color_mode_changes_recompute_preview_styles():
    view = Transcript(Console(file=StringIO()), theme="dark")
    dark = edit_preview_rows("-old\n+new", 40, view.code_theme)
    view.theme = "light"
    light = edit_preview_rows("-old\n+new", 40, view.code_theme)
    assert dark[0][0] != light[0][0]
    view.color_style = "terminal"
    terminal = edit_preview_rows("-old\n+new", 40, view.code_theme)
    assert terminal[0][0] != light[0][0]
    assert "ansibrightred" in terminal[0][0]
    assert "ansigreen" in terminal[1][0]


def test_prompt_preview_uses_colors_and_updates_them_with_the_theme():
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.output.vt100 import Vt100_Output

    from pcode.commands import CommandRegistry
    from pcode.runtime import EditPreview
    from pcode.ui import Activity, create_prompt

    async def run():
        stream = StringIO()
        activity = Activity(edit_previews={"one": EditPreview("one", "x.py", "-old\n+new")})
        view = Transcript(Console(file=stream), activity=activity)
        with create_pipe_input() as pipe:
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=view,
                input=pipe,
                output=Vt100_Output(stream, lambda: Size(rows=24, columns=40), enable_cpr=False),
            )
            app = session.app
            with set_app(app):
                try:
                    for theme, colors in (
                        ("dark", "palette"),
                        ("light", "palette"),
                        ("light", "terminal"),
                    ):
                        view.theme, view.color_style = theme, colors
                        app.renderer.render(app, app.layout)
                        fragments = [
                            fragment
                            for window in app.layout.find_all_windows()
                            if window.render_info is not None
                            and isinstance(window.content, FormattedTextControl)
                            for fragment in to_formatted_text(window.content.text)
                        ]
                        expected = edit_preview_rows("-old\n+new", 38, view.code_theme)
                        for style, text in expected:
                            assert any(f[0] == style and f[1].strip() == text for f in fragments)
                        assert app.layout.current_window.render_info.window_height == 1
                finally:
                    await app.cancel_and_wait_for_background_tasks()

    asyncio.run(run())
