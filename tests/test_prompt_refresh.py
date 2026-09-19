"""Refresh scheduling and cache lifetime on real Application redraws."""

import asyncio
from contextvars import copy_context
from io import StringIO
from unittest.mock import Mock

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from pcode.commands import CommandRegistry
from pcode.runtime import CommandOutput, EditPreview, ToolStarted
from pcode.ui import Activity, Transcript, create_prompt


def test_idle_refresh_stops_and_activity_restarts_it():
    async def run():
        activity = Activity()
        with create_pipe_input() as pipe:
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                input=pipe,
                output=Vt100_Output(
                    StringIO(), lambda: Size(rows=32, columns=100), enable_cpr=False
                ),
            )
            app = session.app
            app.context = copy_context()
            app._is_running = True
            app.invalidate = Mock()
            try:
                assert not app.refresh_interval
                app._redraw()
                await asyncio.sleep(0.15)
                app.invalidate.assert_not_called()
                # Backend startup has no prompt yet; busy must animate too.
                activity.busy = True
                app._redraw()
                await asyncio.sleep(0.15)
                app.invalidate.assert_called_once()
                activity.busy = False
                activity.start_prompt("working")
                app.invalidate.reset_mock()
                app._redraw()
                await asyncio.sleep(0.15)
                app.invalidate.assert_called_once()
                # Cancellation cancels an outstanding tick immediately.
                app._redraw()
                activity.finish_prompt("cancelled")
                app._redraw()
                app.invalidate.reset_mock()
                await asyncio.sleep(0.15)
                app.invalidate.assert_not_called()
                activity.tasks_autohidden = False
                activity.tools.record(ToolStarted("shell", "waiting", call_id="one"))
                app._redraw()
                await asyncio.sleep(0.15)
                app.invalidate.assert_called_once()
                activity.tools.interrupt_running()
                app._redraw()
                app.invalidate.reset_mock()
                await asyncio.sleep(0.15)
                app.invalidate.assert_not_called()
            finally:
                app._is_running = False
                await app.cancel_and_wait_for_background_tasks()

    asyncio.run(run())


def test_preview_cache_keys_and_per_redraw_layout(monkeypatch):
    import pcode.ui as ui

    command_text = Mock(wraps=ui.command_text)
    edit_rows = Mock(wraps=ui.edit_preview_rows)
    monkeypatch.setattr(ui, "command_text", command_text)
    monkeypatch.setattr(ui, "edit_preview_rows", edit_rows)

    async def run():
        stream = StringIO()
        size = Size(rows=32, columns=100)
        activity = Activity()
        activity.command_outputs["one"] = CommandOutput("one", "test", "first\nlast")
        view = Transcript(
            Console(file=stream), activity=activity, preferences={"command_scrollback": "on"}
        )
        with create_pipe_input() as pipe:
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=view,
                on_submit=lambda text: None,
                input=pipe,
                output=Vt100_Output(stream, lambda: size, enable_cpr=False),
            )
            app = session.app
            app.context = copy_context()
            app._is_running = True
            plans = Mock(wraps=activity.plan_rows)
            activity.plan_rows = plans
            try:
                with set_app(app):
                    app._redraw()
                    command_text.assert_called_once()
                    plans.assert_called_once()
                    app._redraw()
                    command_text.assert_called_once()
                    assert plans.call_count == 2
                    # Editor and terminal height are not cross-redraw cached.
                    session.default_buffer.text = "one\ntwo\nthree"
                    size = Size(rows=24, columns=100)
                    app._redraw()
                    assert app.layout.current_window.render_info.window_height == 3
                    command_text.assert_called_once()
                    size = Size(rows=24, columns=40)
                    app._redraw()
                    assert command_text.call_count == 2
                    activity.command_outputs["one"] = CommandOutput("one", "renamed", "new tail")
                    app._redraw()
                    assert command_text.call_count == 3
                    assert "new tail" in stream.getvalue()
                    # Hidden bodies must be evicted, not retained indefinitely.
                    view.command_scrollback = False
                    app._redraw()
                    view.command_scrollback = True
                    app._redraw()
                    assert command_text.call_count == 4
                    activity.edit_previews["edit"] = EditPreview("edit", "x.py", "+hello")
                    app._redraw()
                    edit_rows.assert_called_once()
                    app._redraw()
                    edit_rows.assert_called_once()
                    view.theme = "light"
                    app._redraw()
                    assert edit_rows.call_count == 2
                    activity.edit_previews.clear()
                    app._redraw()
                    assert command_text.call_count == 5
            finally:
                app._is_running = False
                await app.cancel_and_wait_for_background_tasks()

    asyncio.run(run())
