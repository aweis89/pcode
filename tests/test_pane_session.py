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
        b"\x1b_pcode;session;one\x1b\\\x1b_pcode;session;two\x1b\\\x1b_pcode;session;\x1b\\"
    )


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
