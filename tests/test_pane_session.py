import os

from pcode import drawer


def test_session_marker_rejects_unsafe_ids():
    assert drawer.session_marker("abc-123") == "\x1b_pcode;session;abc-123\x1b\\"
    assert drawer.session_marker("bad;id") == "\x1b_pcode;session;\x1b\\"
    assert drawer.session_marker(None) == "\x1b_pcode;session;\x1b\\"


def test_host_marker_rejects_unsafe_ids():
    assert drawer.host_marker("8f3a1b2c") == "\x1b_pcode;host;8f3a1b2c\x1b\\"
    assert drawer.host_marker("not-hex!") == "\x1b_pcode;host;\x1b\\"
    assert drawer.host_marker("8f3a1b2C") == "\x1b_pcode;host;\x1b\\"
    assert drawer.host_marker(None) == "\x1b_pcode;host;\x1b\\"


def test_pane_host_sends_changes_and_clears_on_close():
    current = ["8f3a1b2c"]
    read, write = os.pipe()
    pane = drawer.PaneHost(lambda: current[0], write)
    pane.tick()
    pane.tick()
    current[0] = "1234abcd"
    pane.tick()
    pane.close()
    os.close(write)
    assert os.read(read, 1024) == (
        b"\x1b_pcode;host;8f3a1b2c\x1b\\\x1b_pcode;host;1234abcd\x1b\\\x1b_pcode;host;\x1b\\"
    )


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
        b"\x1b_pcode;session;one\x1b\\\x1b_pcode;session;two\x1b\\\x1b_pcode;session;\x1b\\"
    )


def test_pane_session_sends_nothing_before_a_session_exists():
    """A resuming client ticks before it attaches: an empty marker would wipe the pane's session."""
    current = [None]
    read, write = os.pipe()
    pane = drawer.PaneSession(lambda: current[0], write)
    pane.tick()
    current[0] = "one"
    pane.tick()
    current[0] = None  # /new before its first turn: clear the old one
    pane.tick()
    os.close(write)
    assert os.read(read, 1024) == b"\x1b_pcode;session;one\x1b\\\x1b_pcode;session;\x1b\\"


def test_reconciler_ticks_every_item_and_closes_them():
    import asyncio

    from pcode.terminal_notify import Reconciler

    calls = []

    class Item:
        def __init__(self, name):
            self.name = name

        def tick(self):
            calls.append(("tick", self.name))

        def close(self):
            calls.append(("close", self.name))

    async def main():
        task = asyncio.create_task(Reconciler([Item("a"), Item("b")], interval=0.01).run())
        await asyncio.sleep(0.005)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert calls == [("tick", "a"), ("tick", "b"), ("close", "a"), ("close", "b")]
