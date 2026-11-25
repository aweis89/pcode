"""The docked editor's reserve: pane rows kept clear under the card (`pcode.dock`)."""

import os
import socket
from contextlib import suppress
from types import SimpleNamespace

import pytest

from pcode import dock


class FakePane:
    """pcode's pane, as `sys.stdout`: records what is written to it."""

    def __init__(self) -> None:
        self.text = ""

    def write(self, text: str) -> int:
        self.text += text
        return len(text)

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        return 1

    def take(self) -> str:
        text, self.text = self.text, ""
        return text


@pytest.fixture
def docked(monkeypatch):
    """A dock on a real pty, with pcode's pane 30 rows tall and the cursor known."""
    master, slave = os.openpty()
    ours, theirs = socket.socketpair()
    pane = FakePane()
    size = {"lines": 30}
    # Not `sys.stdout` itself: pytest's capture puts its own back between phases.
    monkeypatch.setattr(dock, "sys", SimpleNamespace(stdout=pane, stdin=FakePane()))
    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((80, size["lines"])))
    monkeypatch.setattr(dock, "_cell_height", lambda fd: None)
    d = dock.Dock(slave, ours)
    d.size = size
    d.pane = pane
    d.pane_rows()
    # The first look at the pane asks where its cursor is.
    assert pane.take() == "\x1b[6n"
    yield d
    for closing in (theirs, ours):
        closing.close()
    os.close(master)
    # `close` closes it already.
    with suppress(OSError):
        os.close(slave)


def answer(d, row: int) -> None:
    """The pane's reply to the cursor question: 0-based ``row``."""
    d._take_reply(f"\x1b[{row + 1};1R".encode())


def test_reserve_rounds_up_and_leaves_the_pane_two_rows():
    assert dock.reserve_for(3, 50) == 5  # 3 + 1.75 rows
    assert dock.reserve_for(10, 50) == 12
    assert dock.reserve_for(3, 50, ratio=1.25) == 6  # 5.9375
    assert dock.reserve_for(40, 20) == 18


def test_region_is_set_once_the_cursor_row_is_known(docked):
    # The card's first three rows hide five pane rows.
    assert docked.reserve == 5
    assert docked.pane_rows() == 25
    answer(docked, 3)
    assert docked.pane.take() == "\x1b[1;25r\x1b[4;1H"
    assert docked.pane_row == 3


def test_cursor_already_under_the_card_scrolls_the_pane_first(docked):
    answer(docked, 28)
    # The whole pane scrolls until the cursor is on the region's last row.
    assert docked.pane.take() == "\x1b[r\x1b[30;1H" + "\n" * 4 + "\x1b[1;25r\x1b[25;1H"
    assert docked.pane_row == 24


def test_rows_written_stop_at_the_region_bottom(docked):
    answer(docked, 20)
    docked.wrote(10)
    assert docked.pane_row == 24


def test_growing_scrolls_before_the_card_is_told(docked):
    answer(docked, 24)
    docked.pane.take()
    glides = []
    docked.glide = lambda rows: (glides.append(rows) or "<before>", "<after>")
    sent = []
    app = SimpleNamespace(
        layout=SimpleNamespace(container=SimpleNamespace(_measured=(0, 0, 5))),
        output=SimpleNamespace(
            write_raw=lambda text: sent.append((text, docked.pane.text)), flush=lambda: None
        ),
    )
    docked.report_rows(app)
    # Five rows hide seven pane rows: two more scroll up from the old bottom.
    scroll = "\x1b[25;1H\n\n\x1b[1;23r\x1b[23;1H"
    assert docked.pane.text == dock.SYNC_START + "<before>" + scroll + "<after>" + dock.SYNC_END
    assert glides == [2]
    # The pane had scrolled by the time the card heard.
    assert sent == [(dock._apc("dock-rows;5"), docked.pane.text)]
    assert docked.pane_row == 22


def test_growing_with_room_to_spare_only_moves_the_region(docked):
    answer(docked, 4)
    docked.pane.take()
    docked._rows = 5
    docked._move_card_edge()
    assert docked.pane.take() == "\x1b[1;23r\x1b[5;1H"
    assert docked.pane_row == 4


def test_growing_with_the_cursor_unknown_scrolls_it_up_inside(docked):
    answer(docked, 24)
    docked.pane.take()
    docked.ask_cursor()
    docked.pane.take()
    docked._rows = 5
    docked._move_card_edge()
    text = docked.pane.take()
    assert text.startswith("\x1b7\x1b[25;1H\n\n\x1b8\x1b[2A\x1b7\x1b[1;23r\x1b8")
    # Its outstanding report predates the scroll: asked again.
    assert docked._requery


def test_shrinking_only_widens_the_region(docked):
    answer(docked, 24)
    docked._rows = 6
    docked._move_card_edge()
    docked.pane.take()
    docked._rows = 3
    docked._move_card_edge()
    assert docked.pane.take() == "\x1b[1;25r\x1b[22;1H"
    assert docked.pane_row == 21


def test_a_resize_drops_the_region_until_the_cursor_is_known(docked):
    answer(docked, 24)
    docked.pane.take()
    docked.size["lines"] = 20
    assert docked.pane_rows() == 15
    assert docked.pane.take() == "\x1b[6n"
    assert docked._region is None
    # Rows written meanwhile scroll the whole pane, as without a region.
    docked.wrote(3)
    answer(docked, 19)
    assert docked.pane.take() == "\x1b[r\x1b[20;1H" + "\n" * 5 + "\x1b[1;15r\x1b[15;1H"


def test_a_replay_sets_the_region_again(docked):
    answer(docked, 10)
    assert docked.homed() == "\x1b[1;25r\x1b[1;1H"
    assert docked.pane_row == 0


def test_close_lets_the_pane_scroll_whole_again(docked):
    answer(docked, 10)
    docked.pane.take()
    docked.close()
    text = docked.pane.take()
    assert "\x1b7\x1b[r\x1b8" in text
    assert text.endswith(dock._apc("dock-close"))
