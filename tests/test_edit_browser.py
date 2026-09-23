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


def test_paging_keys_scroll_the_focused_pane_and_escape_closes():
    async def run():
        long = "\n".join(f"+line {i:03}" for i in range(200))
        with create_pipe_input() as pipe:
            ui = EditBrowser([change("long.py", patch=long)], input=pipe, output=DummyOutput())
            task = asyncio.create_task(ui.run())
            await asyncio.sleep(0.05)
            pipe.send_text("\x1b[6~")  # PageDown in the file list pages the list, not the diff.
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.files)
            assert ui.diff.document.cursor_position_row == 0
            pipe.send_text("\t")
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.diff)
            pipe.send_text("\x1b[6~")
            await asyncio.sleep(0.05)
            scrolled = ui.diff.document.cursor_position_row
            assert scrolled > 0
            pipe.send_text("\x15")  # Ctrl+U
            await asyncio.sleep(0.05)
            assert ui.diff.document.cursor_position_row < scrolled
            pipe.send_text("\x04")  # Ctrl+D scrolls; it no longer closes the browser.
            await asyncio.sleep(0.05)
            assert not task.done()
            pipe.send_text("\x1b")
            await asyncio.wait_for(task, 2)

    asyncio.run(run())


def test_path_search_fuzzy_filters_the_file_list():
    changes = [
        change("src/pcode/edit_ui.py", call_id="1"),
        change("tests/test_edit_browser.py", call_id="2"),
        change("docs/commands.md", call_id="3"),
    ]
    with create_pipe_input() as pipe:
        ui = EditBrowser(changes, input=pipe, output=DummyOutput())
        ui.query.text = "ed_ui"  # Joined word prefixes, not a plain substring.
        assert [c.path for c in ui.visible] == ["src/pcode/edit_ui.py"]
        assert ui.selected.path == "src/pcode/edit_ui.py"
        ui.query.text = "test edit"
        assert [c.path for c in ui.visible] == ["tests/test_edit_browser.py"]
        ui.query.text = "nothing-here"
        assert ui.visible == [] and ui.selected is None
        assert "No matching edits" in ui.files.text and "No matching edits" in ui.diff.text
        ui.query.text = ""
        assert len(ui.visible) == 3


def test_diff_search_filters_changes_and_jumps_between_matching_rows():
    patch = "@@ -1 +3 @@\n-old\n+self.selected_row = 1\n context\n+selected_row += 1"
    changes = [change("a.py", call_id="1", patch=patch), change("b.py", call_id="2")]
    with create_pipe_input() as pipe:
        ui = EditBrowser(changes, input=pipe, output=DummyOutput())
        ui.search("diffs")
        assert ui.scope == "diffs" and ui.app.layout.has_focus(ui.query)
        ui.query.text = "sel_row"
        assert [c.path for c in ui.visible] == ["a.py"]
        rows = ui.diff_rows()
        assert len(rows) == 2 and ui.diff.document.cursor_position_row == rows[0]
        ui.jump(1)
        assert ui.diff.document.cursor_position_row == rows[1]
        ui.jump(1)  # Wraps around.
        assert ui.diff.document.cursor_position_row == rows[0]
        ui.jump(-1)
        assert ui.diff.document.cursor_position_row == rows[1]
        # Switching scope drops the query typed for the other pane.
        ui.search("paths")
        assert ui.query.text == "" and len(ui.visible) == 2


def test_diff_search_matches_the_redacted_text():
    with create_pipe_input() as pipe:
        ui = EditBrowser(
            [change("app.py", patch='+token = "synthetic-secret"')],
            input=pipe,
            output=DummyOutput(),
        )
        ui.search("diffs")
        ui.query.text = "synthetic-secret"
        assert ui.visible == []
        ui.query.text = "redacted"
        assert len(ui.visible) == 1 and ui.diff_rows()


def test_slash_targets_the_focused_pane_and_enter_returns_to_it():
    async def run():
        with create_pipe_input() as pipe:
            ui = EditBrowser([change("x.py")], input=pipe, output=DummyOutput())
            task = asyncio.create_task(ui.run())
            await asyncio.sleep(0.05)
            pipe.send_text("/")
            await asyncio.sleep(0.05)
            assert ui.scope == "paths" and ui.app.layout.has_focus(ui.query)
            pipe.send_text("\r")
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.files)
            pipe.send_text("\t/")
            await asyncio.sleep(0.05)
            assert ui.scope == "diffs" and ui.app.layout.has_focus(ui.query)
            pipe.send_text("new\r")
            await asyncio.sleep(0.05)
            assert ui.app.layout.has_focus(ui.diff) and ui.query.text == "new"
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
