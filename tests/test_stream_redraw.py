"""Streamed deltas redraw at a capped rate; post-render reads reuse the render's measurements."""

import asyncio
from io import StringIO
from time import time

from prompt_toolkit.application import Application
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import Window
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode import ui
from pcode.ui import TerminalOutput, _MeasuredSplit


def test_streamed_deltas_redraw_once_per_interval(monkeypatch):
    async def scenario():
        with create_pipe_input() as pipe:
            app = Application(input=pipe, output=DummyOutput())
            # Pinned: the throttle reads the time prompt_toolkit stamps on each redraw.
            assert hasattr(app, "_last_redraw_time")
            app.loop = asyncio.get_running_loop()
            redraws = []
            monkeypatch.setattr(app, "invalidate", lambda: redraws.append(time()))
            output = TerminalOutput(Console(file=StringIO()), app)
            wait = ui.STREAM_REDRAW_SECONDS + 0.03

            app._last_redraw_time = 0.0
            output.redraw_streamed()  # nothing drawn lately: at once
            assert len(redraws) == 1

            app._last_redraw_time = time()  # a frame just drew
            for _ in range(5):
                output.redraw_streamed()
            assert len(redraws) == 1  # held for the interval's end, once
            await asyncio.sleep(wait)
            assert len(redraws) == 2

            app._last_redraw_time = time()
            output.redraw_streamed()
            app._last_redraw_time += 0.01  # something else drew the delta meanwhile
            await asyncio.sleep(wait)
            assert len(redraws) == 2

    asyncio.run(scenario())


def test_streamed_redraw_outside_the_apps_loop_redraws_at_once(monkeypatch):
    with create_pipe_input() as pipe:
        app = Application(input=pipe, output=DummyOutput())
        redraws = []
        monkeypatch.setattr(app, "invalidate", lambda: redraws.append(1))
        output = TerminalOutput(Console(file=StringIO()), app)
        app._last_redraw_time = time()
        output.redraw_streamed()
        output.redraw_streamed()
    assert len(redraws) == 2


def test_measured_split_keeps_the_height_its_frame_was_measured_at():
    root = _MeasuredSplit([Window(height=3), Window(height=2)])
    root.preferred_height(40, 20)  # between redraws: not this frame's measurement
    root.begin_frame()
    root.write_to_screen(Screen(), MouseHandlers(), WritePosition(0, 0, 40, 5), "", False, None)
    assert root.drawn is None  # the frame was drawn without measuring the root

    root.begin_frame()
    assert root.preferred_height(40, 20).preferred == 5
    root.write_to_screen(Screen(), MouseHandlers(), WritePosition(0, 0, 40, 5), "", False, None)
    assert root.drawn == (40, 20, 5)
    root.preferred_height(30, 10)  # a later read leaves the drawn frame's alone
    assert root.drawn == (40, 20, 5)
