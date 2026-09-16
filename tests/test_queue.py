"""Exercise editable input and serialized submissions with the real prompt loop."""

import asyncio
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.runtime import Message, TextDelta
from pcode.ui import create_prompt


@pytest.mark.parametrize("outcome", ["success", "cancel", "failure"])
def test_edit_and_queue_during_generation(outcome):
    async def run():
        started = asyncio.Event()
        finish = asyncio.Event()
        followup = asyncio.Event()
        calls = []
        active = 0
        output = StringIO()

        class Runtime:
            session = None
            recovery_blocked = ""

            async def stream(self, text):
                nonlocal active
                active += 1
                assert active == 1
                calls.append(text)
                try:
                    if len(calls) == 1:
                        yield TextDelta("**first**\npartial")
                        started.set()
                        await finish.wait()
                        if outcome == "failure":
                            raise RuntimeError("private provider body")
                        yield TextDelta(" done")
                        yield Message("**first**\npartial done")
                    else:
                        yield TextDelta("second answer")
                        yield Message("second answer")
                        followup.set()
                finally:
                    active -= 1

        app = PreviewApp(
            model="test:local", runtime=Runtime(), console=Console(file=output, color_system=None)
        )
        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            async def wait_for(predicate):
                async with asyncio.timeout(5):
                    while not predicate():
                        await asyncio.sleep(0.01)

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait_for(lambda: session is not None and session.app.is_running)
                    pipe.send_text("first\r")
                    await asyncio.wait_for(started.wait(), 5)
                    pipe.send_text("second\rdraft text\x1b[D\x1b[D\x1b[D\x1b[D")
                    await wait_for(lambda: session.default_buffer.text == "draft text")
                    assert session.default_buffer.cursor_position == 6
                    assert app.activity.queued == 1
                    assert calls == ["first"]
                    if outcome == "cancel":
                        pipe.send_text("\x03")
                    else:
                        finish.set()
                    await wait_for(lambda: not app.activity.busy)
                    if outcome == "success":
                        assert followup.is_set()
                        assert calls == ["first", "second"]
                    else:
                        assert calls == ["first"]
                        assert app.activity.queued == 0
                    assert session.default_buffer.text == "draft text"
                    assert session.default_buffer.cursor_position == 6
                    pipe.send_text("my ")
                    await wait_for(lambda: session.default_buffer.text == "draft my text")
                    pipe.send_text("\x03\x04")  # Idle: discard draft, then exit.
                    await asyncio.wait_for(task, 5)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
        printed = output.getvalue()
        assert "**first**" not in printed
        assert printed.count("first partial") == 1  # Rendered once, including cancellation.
        assert printed.count("partial") == 1
        assert "private provider body" not in printed
        if outcome == "cancel":
            assert "Run cancelled" in printed
        elif outcome == "failure":
            assert "Run failed" in printed
        else:
            assert printed.count("second answer") == 1

    asyncio.run(run())
