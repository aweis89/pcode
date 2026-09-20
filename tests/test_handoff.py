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

from pcode.ui import suspended_editor


class FakeApp(Application):
    """Just the handoff's surface; the real constructor needs a terminal."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class Renderer:
    def __init__(self, cpr_replies: bool):
        self.cpr_replies = cpr_replies
        self.futures = []
        self.log = []

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
        output=SimpleNamespace(responds_to_cpr=True),
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


def test_stand_in_app_passes_straight_through():
    app = SimpleNamespace(output=object())

    async def run():
        async with suspended_editor(app):
            pass

    asyncio.run(run())
