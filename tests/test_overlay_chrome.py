"""Popups drawn on a native overlay drop the boxes its panel already draws."""

import base64
import os

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.input import DummyInput
from prompt_toolkit.layout import Layout, Window, to_container
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from pcode import popup_surface
from pcode.frame import (
    Dialog,
    Frame,
    ScrollingTextArea,
    SlimScrollbarMargin,
    frame_rows,
)
from pcode.keyboard_protocol import keyboard_output
from pcode.popup_surface import _OverlayWriter, drawn_in_overlay, title_apc
from pcode.popup_ui import list_pane_height

WIDTH, HEIGHT = 30, 6


@pytest.fixture
def overlay_output():
    """An output drawing on an overlay's tty, as `popup_surface.overlay` makes it."""
    read, write = os.pipe()
    os.set_blocking(read, False)
    writer = _OverlayWriter(write)
    writer.ready = b""
    output = Vt100_Output(writer, lambda: Size(HEIGHT, WIDTH))
    yield output, writer, read
    os.close(read)
    os.close(write)


def body(text="body") -> Window:
    return Window(FormattedTextControl(text))


def render(container, output, width=WIDTH, height=HEIGHT) -> list[str]:
    app = Application(layout=Layout(body()), input=DummyInput(), output=output)
    screen = Screen()
    with set_app(app):
        to_container(container).write_to_screen(
            screen, MouseHandlers(), WritePosition(0, 0, width, height), "", False, None
        )
    return [
        "".join(screen.data_buffer[row][column].char for column in range(width))
        for row in range(height)
    ]


def test_the_body_has_one_place_in_the_tree(overlay_output):
    """Focus and key bindings walk the tree: a body under two parents misleads them."""
    output, _, _ = overlay_output
    inner = body()
    for container in (Frame(inner, title="T"), Dialog(Frame(inner, title="T"), title="D")):
        for case in (output, DummyOutput()):
            app = Application(layout=Layout(container), input=DummyInput(), output=case)
            with set_app(app):
                assert list(app.layout.find_all_windows()).count(inner) == 1


def drained(read: int) -> bytes:
    try:
        return os.read(read, 65536)
    except BlockingIOError:
        return b""


def test_only_an_overlays_output_counts_as_one(overlay_output):
    output, _, _ = overlay_output
    for case, expected in ((output, True), (keyboard_output(output), True), (DummyOutput(), False)):
        app = Application(layout=Layout(body()), input=DummyInput(), output=case)
        with set_app(app):
            assert drawn_in_overlay() is expected
    assert not drawn_in_overlay()


def test_a_full_screen_frame_keeps_its_box():
    frame = Frame(body(), title="Title", footer="Esc close")
    rows = render(frame, DummyOutput())
    assert rows[0] == "┌─ Title " + "─" * (WIDTH - 10) + "┐"
    assert rows[1] == "│body" + " " * (WIDTH - 6) + "│"
    assert rows[-1] == "└" + "─" * (WIDTH - 14) + " Esc close ─┘"


def test_an_overlay_frame_is_a_heading_rule_and_a_footer(overlay_output):
    output, _, _ = overlay_output
    frame = Frame(body(), title="Title", footer="Esc close")
    rows = render(frame, output)
    assert rows[0] == " Title " + "─" * (WIDTH - 8) + " "
    # Its body lines up under the title, one column in from either side.
    assert rows[1] == " body" + " " * (WIDTH - 5)
    # Its footer ends where the body does.
    assert rows[-1] == " " * (WIDTH - 10) + "Esc close "
    assert not any(corner in "".join(rows) for corner in "┌┐└┘│")


def test_an_untitled_overlay_frame_starts_with_its_body(overlay_output):
    output, _, _ = overlay_output
    rows = render(Frame(body()), output)
    assert rows[0] == " body" + " " * (WIDTH - 5)
    assert rows[-1].strip() == ""


def test_a_frame_follows_the_surface_it_is_drawn_on(overlay_output):
    output, _, _ = overlay_output
    frame = Frame(body(), title="Title")
    assert render(frame, output)[0].startswith(" Title ─")
    assert render(frame, DummyOutput())[0].startswith("┌─ Title ─")


def test_a_boxed_frame_stays_boxed_in_an_overlay(overlay_output):
    output, _, _ = overlay_output
    rows = render(Frame(body(), title="Keys", boxed=True), output)
    assert rows[0].startswith("┌─ Keys ─") and rows[0].endswith("┐")


def test_a_dialog_fills_the_overlay_and_titles_its_header(overlay_output):
    output, writer, read = overlay_output
    dialog = Dialog(body(), title="Links")
    rows = render(dialog, output)
    assert rows[0] == "body" + " " * (WIDTH - 4)
    assert not any(corner in "".join(rows) for corner in "┌┐└┘│")
    assert writer.title == "Links"
    # Full screen it keeps its frame and sets no header.
    assert "┌─ Links ─" in render(dialog, DummyOutput())[1]


def test_the_title_is_sent_once_per_change(overlay_output):
    output, writer, read = overlay_output
    dialog = Dialog(body(), title="Links")
    render(dialog, output)
    # Whatever the app wrote setting up, before the title was known.
    drained(read)
    writer._pending.clear()
    writer.write("frame")
    writer.flush()
    assert drained(read) == title_apc("Links") + b"frame"
    render(dialog, output)
    writer.write("frame")
    writer.flush()
    assert drained(read) == b"frame"
    dialog.title = "Links · 3"
    render(dialog, output)
    writer.flush()
    sent = drained(read)
    assert sent == title_apc("Links · 3")
    assert base64.b64decode(sent.split(b";")[2][:-2]).decode() == "Links · 3"


def test_a_long_title_is_cut():
    sent = title_apc("x" * 500)
    payload = sent.removeprefix(b"\x1b_pcode;title;").removesuffix(b"\x1b\\")
    assert base64.b64decode(payload) == b"x" * popup_surface.TITLE_MAX


def test_the_title_goes_out_before_ready():
    read, write = os.pipe()
    try:
        writer = _OverlayWriter(write, "Diffs")
        writer.write("frame")
        writer.flush()
        assert os.read(read, 4096) == title_apc("Diffs") + popup_surface.READY + b"frame"
    finally:
        os.close(read)
        os.close(write)


def scrolled(output, lines: int, cursor: int = 0, height: int = 10) -> list[str]:
    window = Window(
        FormattedTextControl(
            "\n".join(f"line {n}" for n in range(lines)),
            # The window scrolls to keep this row in view.
            get_cursor_position=lambda: Point(0, cursor),
        ),
        right_margins=[SlimScrollbarMargin()],
    )
    rows = render(window, output, width=12, height=height)
    return [row[-1] for row in rows]


def test_an_overlay_scroll_bar_is_a_thin_thumb(overlay_output):
    output, _, _ = overlay_output
    top = scrolled(output, 100)
    assert set(top) == {"▕", " "}
    assert top[0] == "▕" and top[-1] == " "
    bottom = scrolled(output, 100, cursor=99)
    assert bottom[-1] == "▕" and bottom[0] == " "
    # Nothing overflows, nothing drawn.
    assert scrolled(output, 5) == [" "] * 10


def test_a_full_screen_scroll_bar_keeps_its_arrows():
    column = scrolled(DummyOutput(), 100)
    assert column[0] == "^" and column[-1] == "v"


def test_a_scrolling_text_area_uses_the_slim_bar():
    area = ScrollingTextArea(text="x", scrollbar=True)
    assert [type(margin) for margin in area.window.right_margins] == [SlimScrollbarMargin]
    assert ScrollingTextArea(text="x").window.right_margins == []


def test_frame_rows_follow_the_surface(overlay_output):
    output, _, _ = overlay_output
    assert frame_rows() == 2
    app = Application(layout=Layout(body()), input=DummyInput(), output=output)
    with set_app(app):
        assert frame_rows() == 1
        assert frame_rows(footer=True) == 2
        assert list_pane_height(4).preferred == 5
    assert list_pane_height(4).preferred == 6
