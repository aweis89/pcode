import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.preferences import save_preferences
from pcode.ui import Transcript, create_prompt


@pytest.mark.parametrize("mode", [None, "emacs", "vi"])
def test_saved_editing_mode_reaches_prompt(mode):
    if mode is not None:
        save_preferences(editing_mode=mode)
    app = PreviewApp(console=Console(file=StringIO()))
    prompt = SimpleNamespace(app=Mock(run_async=AsyncMock(), output=DummyOutput()))
    with patch("pcode.app.create_prompt", return_value=prompt) as create:
        asyncio.run(app.run_async())
    assert create.call_args.kwargs["vi_mode"] is (mode == "vi")


@pytest.mark.parametrize(
    "vi_mode,keys,expected",
    [
        (True, "hello world\x1b0dw\r", "world"),
        (True, "hello\x1b0iX\r", "Xhello"),
        (True, "hello\x1b\rworld\r", "hello\nworld"),
        (False, "hello\x01X\r", "Xhello"),
    ],
)
def test_editor_bindings(vi_mode, keys, expected):
    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                CommandRegistry(), vi_mode=vi_mode, input=pipe, output=DummyOutput()
            )
            pipe.send_text(keys)
            return await asyncio.wait_for(prompt.prompt_async(), timeout=3)

    assert asyncio.run(run()) == expected


@pytest.mark.parametrize(
    "vi_mode,keys,expected,cursor",
    [
        (True, "one two three\x1bbb", "one two three", 4),
        (True, "hello\x1b0iX", "Xhello", 1),
        (True, "hello\x1b\rworld", "hello\nworld", 11),
        (False, "hello\x01X", "Xhello", 1),
    ],
)
def test_transcript_editor_bindings(vi_mode, keys, expected, cursor):
    async def run():
        with create_pipe_input() as pipe:
            submitted = []
            snapshots = []

            def submit(text):
                submitted.append(text)
                snapshots.append(
                    (prompt.default_buffer.cursor_position, prompt.app.vi_state.input_mode)
                )
                prompt.app.exit()

            prompt = create_prompt(
                CommandRegistry(),
                vi_mode=vi_mode,
                input=pipe,
                output=DummyOutput(),
                transcript=Transcript(Console(file=StringIO())),
                on_submit=submit,
            )
            assert prompt.app.editing_mode == (EditingMode.VI if vi_mode else EditingMode.EMACS)
            pipe.send_text(keys + "\r")
            await asyncio.wait_for(prompt.app.run_async(), timeout=3)
            assert submitted == [expected]
            assert snapshots[-1][0] == cursor
            if keys.endswith("bb"):
                assert snapshots[-1][1] == InputMode.NAVIGATION

    asyncio.run(run())
