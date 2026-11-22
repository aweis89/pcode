import os

from pcode import drawer


def test_session_marker_rejects_unsafe_ids():
    assert drawer.session_marker("abc-123") == "\x1b_pcode;session;abc-123\x1b\\"
    assert drawer.session_marker("bad;id") == "\x1b_pcode;session;\x1b\\"
    assert drawer.session_marker(None) == "\x1b_pcode;session;\x1b\\"


def test_pane_session_sends_changes_and_clears_on_close():
    current = ["one"]
    read, write = os.pipe()
    pane = drawer.PaneSession(lambda: current[0], write)
    pane.tick()
    pane.tick()  # unchanged: nothing sent
    current[0] = "two"
    pane.tick()
    pane.close()
    pane.close()  # already cleared
    os.close(write)
    assert os.read(read, 1024) == (
        b"\x1b_pcode;session;one\x1b\\"
        b"\x1b_pcode;session;two\x1b\\"
        b"\x1b_pcode;session;\x1b\\"
    )
