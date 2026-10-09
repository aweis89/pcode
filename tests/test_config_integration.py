"""The settings browser uses the terminal command and popup lifecycle."""

import asyncio
from io import StringIO

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.config_ui import ConfigBrowser
from pcode.preferences import read_preferences
from pcode.ui import create_prompt


async def wait_for(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.01)


def test_config_browser_saves_layout_and_returns_to_prompt(monkeypatch, tmp_path):
    async def run():
        app = PreviewApp(workspace=tmp_path, console=Console(file=StringIO()))
        session = None
        browser = None

        def make_browser(**kwargs):
            nonlocal browser
            browser = ConfigBrowser(**kwargs)
            return browser

        monkeypatch.setattr("pcode.config_ui.ConfigBrowser", make_browser)
        with create_pipe_input() as pipe:

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            monkeypatch.setattr("pcode.app.create_prompt", prompt)
            task = asyncio.create_task(app.run_async())
            try:
                await wait_for(lambda: session is not None and session.app.is_running)
                pipe.send_text("/config\r")
                await wait_for(lambda: browser is not None and browser.app.is_running)
                pipe.send_text("tool_max_lines\r\x01\x0b7\r")
                await wait_for(lambda: read_preferences().get("tool_max_lines") == "7")
                assert app.activity.tool_max_rows == 7
                assert "immediately" in browser.message
                assert browser.app.is_running
                pipe.send_text("\x03")
                await wait_for(lambda: not browser.app.is_running and session.app.is_running)
                pipe.send_text("/config get tool_max_lines\r")
                await wait_for(lambda: app._command_popup_generation is None)
                pipe.send_text("/quit\r")
                await asyncio.wait_for(task, 10)
                assert not app.config_requested
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_config_browser_open_error_returns_to_prompt(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    output = StringIO()
    app = PreviewApp(workspace=tmp_path, console=Console(file=output))

    @asynccontextmanager
    async def popup(*args):
        yield None

    def broken_browser(**kwargs):
        raise ValueError("Invalid preferences JSON")

    monkeypatch.setattr(app, "popup", popup)
    monkeypatch.setattr("pcode.config_ui.ConfigBrowser", broken_browser)
    app.prompt_session = SimpleNamespace(app=SimpleNamespace(output=None, style=None))
    asyncio.run(app.run_command("/config", idle=True, tag=None))
    assert not app.config_requested
    assert "Could not open configuration: Invalid preferences JSON" in output.getvalue()
    assert app._command_popup_generation is None
