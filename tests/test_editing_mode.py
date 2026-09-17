import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import CommandRegistry
from pcode.preferences import save_preferences
from pcode.ui import create_prompt


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
