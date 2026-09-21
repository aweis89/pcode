import asyncio
from unittest.mock import patch

import pytest
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput
from rich.text import Text

from pcode.popup_ui import RichPane, fuzzy_match


@pytest.mark.parametrize(
    ("term", "text", "expected"),
    [
        ("edit_ui", "src/pcode/edit_ui.py", True),  # Plain substring.
        ("ed_ui", "src/pcode/edit_ui.py", True),  # Separators split the query into prefixes.
        ("pc/edui", "src/pcode/edit_ui.py", True),
        ("edui", "src/pcode/edit_ui.py", True),
        ("dtui", "src/pcode/edit_ui.py", False),  # Gaps inside a word do not count.
        ("sel_row", "+self.selected_row = 1", True),
        ("_", "anything", False),
    ],
)
def test_fuzzy_match_uses_substrings_or_joined_word_prefixes(term, text, expected):
    assert fuzzy_match(term, text) is expected


def test_rich_pane_scroll_reuses_prepared_lines():
    pane = RichPane()
    pane.set([Text("\n".join(f"Line {i}" for i in range(1000)))])
    first = pane.control.create_content(80, 20)
    with patch("pcode.popup_ui.split_lines", side_effect=AssertionError("Resplit content")):
        for row in range(10):
            pane.window.vertical_scroll = row
            pane.control.preferred_width(80)
            assert pane.control.preferred_height(80, 20, False, None) == 1000
            content = pane.control.create_content(80, 20)
            assert content.get_line(500) is first.get_line(500)
            assert content.cursor_position.y == row
            # Wheel events should go straight to Window, not scan the text.
            assert (
                pane.control.mouse_handler(
                    MouseEvent(
                        Point(0, row), MouseEventType.SCROLL_DOWN, MouseButton.NONE, frozenset()
                    )
                )
                is NotImplemented
            )


def test_rich_pane_invalidates_lines_on_resize_and_content_change():
    pane = RichPane()
    pane.set([Text("abcdefghij" * 4)])
    wide = pane.control.create_content(40, 10)
    narrow = pane.control.create_content(10, 10)
    assert wide.line_count == 1
    assert narrow.line_count == 4
    pane.window.vertical_scroll = 3
    pane.set([Text("replacement")])
    updated = pane.control.create_content(40, 10)
    assert pane.window.vertical_scroll == 0
    assert updated.line_count == 1
    assert "replacement" == "".join(text for _, text in updated.get_line(0))


@pytest.mark.parametrize("kind", ["sessions", "tools"])
def test_popup_content_half_pages_and_full_pages(tmp_path, kind):
    from pcode.inspection import ToolArchive
    from pcode.inspector_ui import ToolInspector
    from pcode.session_ui import SessionBrowser

    async def run():
        with create_pipe_input() as pipe:
            options = {"input": pipe, "output": DummyOutput()}
            if kind == "sessions":
                popup = SessionBrowser([], root=tmp_path, workspace=tmp_path, **options)
            else:
                popup = ToolInspector(ToolArchive(), **options)
            popup.list.buffer.set_document(
                Document("\n".join(f"Row {i}" for i in range(300)), 0), bypass_readonly=True
            )
            popup.detail.set([Text("\n".join(f"Line {i}" for i in range(300)))])
            popup.app.layout.focus(popup.detail.window)
            task = asyncio.create_task(popup.app.run_async())
            try:
                await asyncio.sleep(0.05)
                window = popup.detail.window
                height = window.render_info.window_height

                async def press(keys):
                    pipe.send_text(keys)
                    await asyncio.sleep(0.05)
                    assert not task.done(), "Scrolling must not close the popup"

                await press("\x04")  # Ctrl+D, half page down.
                assert window.vertical_scroll == max(1, height // 2)
                await press("\x15")  # Ctrl+U, half page up.
                assert window.vertical_scroll == 0
                await press("\x1b[6~")  # Page Down.
                assert window.vertical_scroll == height - 1
                await press("\x1b[6~")
                await press("\x1b[5~")  # Page Up goes back one page, not to the top.
                assert window.vertical_scroll == height - 1
                await press("\x1b[B")
                assert window.vertical_scroll == height
                await press("\x1b[A")
                assert window.vertical_scroll == height - 1
                await press("\x04" * 50)
                assert window.vertical_scroll == 300 - height
                await press("\x15" * 50)
                assert window.vertical_scroll == 0
                # The same keys half-page the list, whether it or the query has focus.
                for focus in (popup.list, popup.query):
                    popup.app.layout.focus(focus)
                    await asyncio.sleep(0.05)
                    rows = popup.list.window.render_info.window_height
                    half = max(1, rows // 2)
                    start = popup.list.document.cursor_position_row
                    await press("\x04")
                    assert popup.list.document.cursor_position_row == start + half
                    await press("\x15")
                    assert popup.list.document.cursor_position_row == start
                assert popup.query.text == "", "Paging keys must not type into the query"
                pipe.send_text("\x1b")
                await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    popup.app.exit()
                    await task

    asyncio.run(run())
