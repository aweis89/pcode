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
        (True, "hello\x1bo world\r", "hello\n world"),
        (True, "hello\x1bOworld\r", "world\nhello"),
        (True, "hello\nworld\r", "hello\nworld"),
        (False, "hello\nworld\r", "hello\nworld"),
        (False, "hello\x1b\rworld\r", "hello\nworld"),
        (True, "hello\x1b\r", "hello"),
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
        (True, "hello\nworld", "hello\nworld", 11),
        (False, "hello\nworld", "hello\nworld", 11),
        (False, "hello\x1b\rworld", "hello\nworld", 11),
        (True, "hello\x1b", "hello", 4),
        (True, "hello\x1b[D!", "hell!o", 5),
        (True, "hello\x1b0vll\x1biX", "heXllo", 3),
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


@pytest.mark.parametrize("transcript", [False, True])
@pytest.mark.parametrize("vi_mode", [False, True])
@pytest.mark.parametrize(
    "newline",
    ["\n", "\x1b[106;5u", "\x1b[27;5;106~", "\x1b[13;2u", "\x1b[27;2;13~"],
)
def test_terminal_newline_encodings(transcript, vi_mode, newline):
    async def run():
        with create_pipe_input() as pipe:
            submitted = []

            def submit(text):
                submitted.append(text)
                prompt.app.exit()

            prompt = create_prompt(
                CommandRegistry(),
                vi_mode=vi_mode,
                input=pipe,
                output=DummyOutput(),
                transcript=Transcript(Console(file=StringIO())) if transcript else None,
                on_submit=submit if transcript else None,
            )
            pipe.send_text("first" + newline + "second\r")
            if transcript:
                await asyncio.wait_for(prompt.app.run_async(), timeout=3)
                assert submitted == ["first\nsecond"]
            else:
                assert await asyncio.wait_for(prompt.prompt_async(), timeout=3) == "first\nsecond"

    asyncio.run(run())


DOWN = "\x1b[B"


@pytest.mark.parametrize("vi_mode", [False, True])
@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        # On the last line with nothing else for Down to do, it opens a new line.
        ("first" + DOWN + "second\r", "first\nsecond"),
        # On an earlier line Down still moves the cursor instead of inserting.
        ("ab\ncd" + "\x1b[A" + DOWN + "X\r", "ab\ncdX"),
    ],
)
def test_down_arrow_newline(vi_mode, keys, expected):
    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                CommandRegistry(), vi_mode=vi_mode, input=pipe, output=DummyOutput()
            )
            pipe.send_text(keys)
            assert await asyncio.wait_for(prompt.prompt_async(), timeout=3) == expected

    asyncio.run(run())


def test_down_arrow_in_vi_normal_mode_does_not_insert():
    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                CommandRegistry(), vi_mode=True, input=pipe, output=DummyOutput()
            )
            pipe.send_text("first\x1b" + DOWN + "asecond\r")
            assert await asyncio.wait_for(prompt.prompt_async(), timeout=3) == "firstsecond"

    asyncio.run(run())


@pytest.mark.parametrize("transcript", [False, True])
def test_vi_escape_alone_is_responsive(transcript):
    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                CommandRegistry(),
                vi_mode=True,
                input=pipe,
                output=DummyOutput(),
                transcript=Transcript(Console(file=StringIO())) if transcript else None,
            )
            # A long key-binding timeout must not delay the eager Escape binding.
            prompt.app.timeoutlen = 10
            assert prompt.app.ttimeoutlen == 0.1

            async def feed():
                pipe.send_text("hello\x1b")
                while prompt.app.vi_state.input_mode != InputMode.NAVIGATION:
                    await asyncio.sleep(0.01)
                assert prompt.default_buffer.text == "hello"
                assert prompt.default_buffer.cursor_position == 4
                prompt.app.exit()

            await asyncio.wait_for(
                prompt.app.run_async(pre_run=lambda: prompt.app.create_background_task(feed())),
                timeout=2,
            )

    asyncio.run(run())
