import asyncio
from io import StringIO

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.styles import Style
from pygments.token import Generic
from rich.console import Console
from rich.syntax import Syntax

from pcode.app import PreviewApp
from pcode.edit_transcript import DiffLexer
from pcode.edit_ui import EditBrowser
from pcode.runtime import EditCompleted


def change(path, call_id="one", patch="@@ -1 +1 @@\n-old\n+new", **kwargs):
    return EditCompleted(call_id, path, "edited", patch, added=1, removed=1, **kwargs)


def test_newest_file_is_selected_and_diff_follows_the_selection():
    changes = [change(f"file_{i}.py", call_id=str(i)) for i in range(3)]
    with create_pipe_input() as pipe:
        ui = EditBrowser(changes, input=pipe, output=DummyOutput())
        assert ui.selected.path == "file_2.py"
        assert "file_2.py" in ui.diff.text and "-old" in ui.diff.text
        assert ui.files.text.splitlines() == [
            line for line in ui.files.text.splitlines() if "file_" in line
        ]
        ui.files.buffer.cursor_position = ui.files.document.translate_row_col_to_index(2, 0)
        assert ui.selected.path == "file_0.py"
        assert "file_0.py" in ui.diff.text
        assert ui.diff.window.vertical_scroll == 0


def test_unavailable_and_truncated_diffs_are_explained():
    with create_pipe_input() as pipe:
        ui = EditBrowser(
            [
                change("binary.bin", patch="", omitted="Binary content"),
                change("big.py", truncated=True),
            ],
            input=pipe,
            output=DummyOutput(),
        )
        assert "additional diff rows omitted" in ui.diff.text
        ui.files.buffer.cursor_position = ui.files.document.translate_row_col_to_index(1, 0)
        assert "Diff unavailable: Binary content" in ui.diff.text


def test_empty_conversation_shows_a_notice_without_a_selection():
    with create_pipe_input() as pipe:
        ui = EditBrowser([], input=pipe, output=DummyOutput())
        assert ui.selected is None
        assert "No file edits" in ui.diff.text and "No file edits" in ui.files.text
        assert ui.position() == 0


def test_secrets_are_redacted_before_display():
    with create_pipe_input() as pipe:
        ui = EditBrowser(
            [change("app.py", patch='+token = "synthetic-secret"')],
            input=pipe,
            output=DummyOutput(),
        )
        assert "synthetic-secret" not in ui.diff.text
        assert "[redacted]" in ui.diff.text


def test_lexer_colors_match_the_scrollback_diff_theme():
    document = Document("@@ -1 +1 @@\n-old\n+new\n context")
    line = DiffLexer("monokai").lex_document(document)
    theme = Syntax.get_theme("monokai")
    expected = [Generic.Subheading, Generic.Deleted, Generic.Inserted]
    for row, token in enumerate(expected):
        style = line(row)[0][0]
        color = theme.get_style_for_token(token).color.get_truecolor().hex.lstrip("#")
        assert Style.from_dict({}).get_attrs_for_style_str(style).color == color
    assert line(1)[0][0] != line(2)[0][0]
    assert not Style.from_dict({}).get_attrs_for_style_str(line(3)[0][0]).bgcolor


def test_keyboard_scrolls_the_diff_from_the_file_pane_and_closes():
    async def run():
        long = "\n".join(f"+line {i:03}" for i in range(200))
        with create_pipe_input() as pipe:
            ui = EditBrowser([change("long.py", patch=long)], input=pipe, output=DummyOutput())
            task = asyncio.create_task(ui.run())
            await asyncio.sleep(0.05)
            pipe.send_text("\x1b[6~")  # PageDown, with the file list focused
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.files)
            scrolled = ui.diff.document.cursor_position_row
            assert scrolled > 0
            pipe.send_text("\x1b[5~")  # PageUp
            await asyncio.sleep(0.05)
            assert ui.diff.document.cursor_position_row < scrolled
            pipe.send_text("\t")
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.diff)
            pipe.send_text("\x1b")
            await asyncio.wait_for(task, 2)

    asyncio.run(run())


def test_file_list_keeps_its_rows_when_the_diff_is_long():
    """A long diff must not squeeze the Files pane down to its minimum height."""

    async def run():
        long = "\n".join(f"+line {i:03}" for i in range(500))
        changes = [change(f"file_{i}.py", call_id=str(i), patch=long) for i in range(10)]
        with create_pipe_input() as pipe:
            ui = EditBrowser(
                changes,
                input=pipe,
                output=Vt100_Output(
                    StringIO(), lambda: Size(rows=24, columns=80), enable_cpr=False
                ),
            )
            with set_app(ui.app):
                ui.app.renderer.render(ui.app, ui.app.layout)
                assert ui.files.window.render_info.window_height == 6
                assert ui.diff.window.render_info.window_height > 6

    asyncio.run(run())


def test_slash_command_dispatches_and_collects_changes():
    app = PreviewApp(console=Console(file=StringIO()))
    assert not app.diffs_requested
    app.handle("/diffs")
    assert app.diffs_requested
    app.handle("/diffs unexpected")
    edit = change("kept.py")
    app.present_events((edit,))
    assert app.recorded_edits() == [edit]
    app.new("")
    assert app.recorded_edits() == []
