"""The terminal handoff must repaint the editor once, after the cursor report.

prompt_toolkit's ``in_terminal`` repaints before its cursor position report
arrives, so the editor lands under the new output and then jumps to the bottom
of the screen when the report is processed: a visible flash on every write
while the transcript leaves rows free below it.
"""

import asyncio
from asyncio import Future
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size

from pcode.ui import SYNC_END, SYNC_START, suspended_editor


class FakeApp(Application):
    """Just the handoff's surface; the real constructor needs a terminal."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)


SIZE = Size(rows=40, columns=80)


class Renderer:
    def __init__(self, cpr_replies: bool):
        self.cpr_replies = cpr_replies
        self.futures = []
        self.log = []
        # State a real renderer holds after its last cursor report and paint.
        self._in_alternate_screen = False
        self._min_available_height = 0
        self._last_size = None
        self.rows_above_layout = 0

    def erase(self):
        self.log.append("erase")

    def reset(self):
        self.log.append("reset")

    async def wait_for_cpr_responses(self):
        for future in self.futures:
            await future
        self.futures.clear()

    def request_cpr(self):
        self.log.append("cpr")
        future = Future()
        self.futures.append(future)
        if self.cpr_replies:
            # The reply arrives on the input reader one loop turn later.
            asyncio.get_running_loop().call_soon(self.reply, future)

    def reply(self, future):
        self.log.append("report")
        future.set_result(None)


@contextmanager
def noop():
    yield


def fake_app(cpr_replies: bool, running: bool = True):
    renderer = Renderer(cpr_replies)
    app = FakeApp(
        _is_running=running,
        _running_in_terminal=False,
        _running_in_terminal_f=None,
        renderer=renderer,
        output=SimpleNamespace(
            responds_to_cpr=True,
            get_size=lambda: SIZE,
            write_raw=lambda data: renderer.log.append(data),
            flush=lambda: None,
        ),
        input=SimpleNamespace(detach=noop, cooked_mode=noop),
    )
    app._request_absolute_cursor_position = renderer.request_cpr

    def redraw():
        if not app._running_in_terminal:
            renderer.log.append("paint")

    app._redraw = redraw
    app.invalidate = redraw  # As invalidate() would eventually do.
    return app


def test_editor_is_painted_once_after_the_cursor_report():
    app = fake_app(cpr_replies=True)

    async def run():
        async with suspended_editor(app):
            app.renderer.log.append("write")
            assert app._running_in_terminal
        assert not app._running_in_terminal

    asyncio.run(run())
    assert app.renderer.log == ["erase", "write", "reset", "cpr", "report", "paint"]


def test_invalidation_while_awaiting_the_report_does_not_paint_early():
    app = fake_app(cpr_replies=True)

    async def run():
        async with suspended_editor(app):
            # A spinner tick or keystroke scheduled during the handoff.
            asyncio.get_running_loop().call_soon(app.invalidate)

    asyncio.run(run())
    assert app.renderer.log.count("paint") == 1
    assert app.renderer.log[-2:] == ["report", "paint"]


@pytest.mark.parametrize("atomic", [False, True])
def test_only_a_non_atomic_handoff_puts_the_terminal_in_cooked_mode(atomic):
    """Cooked mode echoes typing and turns Return into the editor's Ctrl+J.

    Only an external program needs it, and paced scrollback makes atomic
    handoffs frequent while the user may be typing.
    """
    app = fake_app(cpr_replies=True)
    entered = []

    @contextmanager
    def cooked_mode():
        entered.append("cooked")
        yield

    app.input = SimpleNamespace(detach=noop, cooked_mode=cooked_mode)

    async def run():
        async with suspended_editor(app, atomic=atomic) as handoff:
            handoff.rows_written = 1

    asyncio.run(run())
    assert entered == ([] if atomic else ["cooked"])


def test_failure_inside_the_handoff_still_restores_the_editor():
    app = fake_app(cpr_replies=True)

    async def run():
        with pytest.raises(RuntimeError):
            async with suspended_editor(app):
                raise RuntimeError("print failed")

    asyncio.run(run())
    assert not app._running_in_terminal
    assert app.renderer.log[-1] == "paint"
    assert app._running_in_terminal_f.done()


def test_handoffs_chain_in_order():
    app = fake_app(cpr_replies=True)

    async def run():
        async def one(name):
            async with suspended_editor(app):
                app.renderer.log.append(name)

        await asyncio.gather(one("first"), one("second"))

    asyncio.run(run())
    assert app.renderer.log == [
        "erase", "first", "reset", "cpr", "report", "paint",
        "erase", "second", "reset", "cpr", "report", "paint",
    ]  # fmt: skip


def painted_app(cpr_replies: bool = True, rows_above: int = 10):
    """An app whose renderer knows where the editor sits, as after one report."""
    app = fake_app(cpr_replies)
    app.renderer._min_available_height = SIZE.rows - rows_above
    app.renderer._last_size = SIZE
    app.renderer.rows_above_layout = rows_above
    return app


def test_atomic_handoff_repaints_at_once_inside_one_synchronized_frame():
    app = painted_app()

    async def run():
        async with suspended_editor(app, atomic=True) as handoff:
            assert handoff.top_row == 11
            app.renderer.log.append("write")
            handoff.rows_written = 5

    asyncio.run(run())
    # Painted straight after the write, before the report; nothing awaited.
    assert app.renderer.log == [
        SYNC_START, "erase", "write", "reset", "cpr", "paint", SYNC_END, "report"
    ]  # fmt: skip
    # The renderer was told the rows left below row 16 without asking.
    assert app.renderer._min_available_height == SIZE.rows - 16 + 1


def test_atomic_handoff_clamps_the_cursor_to_the_last_row():
    app = painted_app(rows_above=30)

    async def run():
        async with suspended_editor(app, atomic=True) as handoff:
            handoff.rows_written = 500  # scrolled the screen

    asyncio.run(run())
    assert app.renderer._min_available_height == 1


def test_atomic_handoff_honours_a_body_that_homed_the_cursor():
    app = painted_app(rows_above=30)

    async def run():
        async with suspended_editor(app, atomic=True) as handoff:
            handoff.top_row = 1  # after a clear-and-home
            handoff.rows_written = 3

    asyncio.run(run())
    assert app.renderer._min_available_height == SIZE.rows - 4 + 1


def test_next_handoff_waits_for_the_outstanding_report_before_cooked_mode():
    app = painted_app()

    async def run():
        async with suspended_editor(app, atomic=True) as handoff:
            handoff.rows_written = 1
        async with suspended_editor(app, atomic=True) as handoff:
            handoff.rows_written = 1

    asyncio.run(run())
    first_report = app.renderer.log.index("report")
    second_erase = app.renderer.log.index("erase", first_report)
    assert first_report < second_erase


@pytest.mark.parametrize("reason", ["no_report_yet", "resized", "rows_unknown"])
def test_atomic_handoff_falls_back_to_the_report_when_the_row_is_unknown(reason):
    app = painted_app()
    if reason == "no_report_yet":
        app.renderer._min_available_height = 0
    elif reason == "resized":
        app.renderer._last_size = Size(rows=24, columns=80)

    async def run():
        async with suspended_editor(app, atomic=True) as handoff:
            app.renderer.log.append("write")
            if reason != "rows_unknown":
                handoff.rows_written = 2

    asyncio.run(run())
    # The frame is released before the round trip, not held across it.
    assert app.renderer.log == [
        SYNC_START, "erase", "write", "reset", SYNC_END, "cpr", "report", "paint"
    ]  # fmt: skip


def test_atomic_handoff_failure_still_propagates_and_restores_the_editor():
    app = painted_app()

    async def run():
        with pytest.raises(RuntimeError):
            async with suspended_editor(app, atomic=True) as handoff:
                handoff.rows_written = 1
                raise RuntimeError("print failed")

    asyncio.run(run())
    assert not app._running_in_terminal
    assert app.renderer.log[-3:] == ["paint", SYNC_END, "report"]


def test_stand_in_app_passes_straight_through():
    app = SimpleNamespace(output=object())

    async def run():
        async with suspended_editor(app):
            pass

    asyncio.run(run())
