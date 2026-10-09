"""Extended keyboard mode follows renderer ownership, not just process lifetime."""

import asyncio
import subprocess
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from pcode.commands import CommandRegistry
from pcode.keyboard_protocol import (
    KEYBOARD_POP,
    KEYBOARD_PUSH,
    TMUX_ENABLE,
    TMUX_RESTORE,
    KeyboardProtocolOutput,
    keyboard_output,
)
from pcode.ui import Transcript, create_prompt, suspended_editor


@pytest.fixture(autouse=True)
def outside_tmux(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)


class TerminalStream(StringIO):
    def isatty(self):
        return True


def terminal_output(stream):
    return Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)


def test_negotiation_only_on_terminal_output():
    dummy = DummyOutput()
    redirected = terminal_output(StringIO())
    assert keyboard_output(dummy) is dummy
    assert keyboard_output(redirected) is redirected
    wrapped = keyboard_output(terminal_output(TerminalStream()))
    assert isinstance(wrapped, KeyboardProtocolOutput)
    assert keyboard_output(wrapped) is wrapped


@pytest.mark.parametrize("mode", ["VT10x", "Ext 1", "Ext 2", "unknown"])
def test_tmux_negotiation_snapshots_once_and_preserves_inherited_mode(monkeypatch, mode):
    monkeypatch.setenv("TMUX_PANE", "%42")
    query = Mock(return_value=SimpleNamespace(stdout=mode + "\n"))
    monkeypatch.setattr("pcode.keyboard_protocol.subprocess.run", query)
    stream = TerminalStream()
    output = keyboard_output(terminal_output(stream))
    for _ in range(2):
        output.enable_bracketed_paste()
        output.quit_alternate_screen()
        output.disable_bracketed_paste()
    output.flush()
    query.assert_called_once_with(
        ["tmux", "display-message", "-p", "-t", "%42", "#{pane_key_mode}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=0.5,
    )
    text = stream.getvalue()
    assert KEYBOARD_PUSH not in text and KEYBOARD_POP not in text
    assert text.count(TMUX_ENABLE) == (2 if mode == "VT10x" else 0)
    assert text.count(TMUX_RESTORE) == (2 if mode == "VT10x" else 0)
    if mode == "VT10x":
        assert text.index(TMUX_RESTORE) < text.index("\x1b[?1049l")


@pytest.mark.parametrize("failure", [OSError(), subprocess.TimeoutExpired("tmux", 0.5)])
def test_tmux_query_failure_does_not_guess_restore_mode(monkeypatch, failure):
    monkeypatch.setenv("TMUX_PANE", "%42")
    monkeypatch.setattr("pcode.keyboard_protocol.subprocess.run", Mock(side_effect=failure))
    stream = TerminalStream()
    output = keyboard_output(terminal_output(stream))
    output.enable_bracketed_paste()
    output.disable_bracketed_paste()
    output.flush()
    assert TMUX_ENABLE not in stream.getvalue()
    assert TMUX_RESTORE not in stream.getvalue()
    assert KEYBOARD_PUSH not in stream.getvalue()


@pytest.mark.parametrize("alternate", [False, True])
def test_balanced_keyboard_stack_and_screen_order(alternate):
    stream = TerminalStream()
    output = keyboard_output(terminal_output(stream))
    # Renderer construction/reset before its first frame must not pop a stack.
    output.disable_bracketed_paste()
    if alternate:
        output.enter_alternate_screen()
    output.enable_bracketed_paste()
    output.enable_bracketed_paste()
    if alternate:
        output.quit_alternate_screen()
    output.disable_bracketed_paste()
    output.disable_bracketed_paste()
    output.flush()
    text = stream.getvalue()
    assert text.count(KEYBOARD_PUSH) == text.count(KEYBOARD_POP) == 1
    if alternate:
        assert text.index("\x1b[?1049h") < text.index(KEYBOARD_PUSH)
        assert text.index(KEYBOARD_POP) < text.index("\x1b[?1049l")
    # A subsequent render reacquires the mode, rather than leaving it disabled.
    output.enable_bracketed_paste()
    output.disable_bracketed_paste()
    output.flush()
    assert stream.getvalue().count(KEYBOARD_PUSH) == 2
    assert stream.getvalue().count(KEYBOARD_POP) == 2


@pytest.mark.parametrize("fail", [False, True])
def test_real_renderer_restores_keyboard_on_handoff_and_exit(fail):
    async def run():
        stream = TerminalStream()
        with create_pipe_input() as pipe:
            prompt = create_prompt(CommandRegistry(), input=pipe, output=terminal_output(stream))

            def check_released():
                text = stream.getvalue()
                assert text.count(KEYBOARD_PUSH) > 0
                assert text.count(KEYBOARD_PUSH) == text.count(KEYBOARD_POP)

            async def exercise():
                # Wait for rendering, not a fixed delay before terminal ownership.
                while KEYBOARD_PUSH not in stream.getvalue():
                    await asyncio.sleep(0)
                await run_in_terminal(check_released)
                async with suspended_editor(prompt.app):
                    check_released()
                prompt.app.output.flush()
                text = stream.getvalue()
                assert text.count(KEYBOARD_PUSH) == text.count(KEYBOARD_POP) + 1
                if fail:
                    prompt.app.exit(exception=RuntimeError("test exit"))
                else:
                    pipe.send_text("one\x1b[13;2utwo\r")

            def start():
                prompt.app.create_background_task(exercise())

            if fail:
                with pytest.raises(RuntimeError, match="test exit"):
                    await asyncio.wait_for(prompt.app.run_async(pre_run=start), 3)
            else:
                assert await asyncio.wait_for(prompt.app.run_async(pre_run=start), 3) == "one\ntwo"
            check_released()

    asyncio.run(run())


@pytest.mark.parametrize("tmux", [False, True])
@pytest.mark.parametrize("known_position", [False, True])
@pytest.mark.parametrize("failure", [None, "body", "cancel", "repaint"])
def test_atomic_handoff_preserves_keyboard_until_repaint(
    monkeypatch, tmux, known_position, failure
):
    if tmux:
        monkeypatch.setenv("TMUX_PANE", "%42")
        monkeypatch.setattr(
            "pcode.keyboard_protocol.subprocess.run",
            Mock(return_value=SimpleNamespace(stdout="VT10x\n")),
        )
    enable, disable = (TMUX_ENABLE, TMUX_RESTORE) if tmux else (KEYBOARD_PUSH, KEYBOARD_POP)

    async def run():
        stream = TerminalStream()
        with create_pipe_input() as pipe:
            prompt = create_prompt(CommandRegistry(), input=pipe, output=terminal_output(stream))
            app = prompt.app
            painted = asyncio.Event()
            app.after_render += lambda _: painted.set()
            waiting = asyncio.Event()
            release = asyncio.Event()
            waits = 0

            def check_preserved():
                app.output.flush()
                text = stream.getvalue()
                assert text.count(enable) == 1
                assert disable not in text

            async def wait_for_cpr():
                nonlocal waits
                waits += 1
                if waits == 2 and not known_position:
                    waiting.set()
                    await release.wait()

            async def handoff():
                async with suspended_editor(app, atomic=True) as state:
                    check_preserved()
                    state.rows_written = 0
                    if failure == "body":
                        raise RuntimeError("body failed")
                    if failure == "cancel" and known_position:
                        raise asyncio.CancelledError

            async def exercise():
                try:
                    await painted.wait()
                    monkeypatch.setattr(
                        "pcode.ui.layout_top_row", lambda _: 1 if known_position else None
                    )
                    monkeypatch.setattr(app.output, "responds_to_cpr", True)
                    monkeypatch.setattr(app.renderer, "wait_for_cpr_responses", wait_for_cpr)
                    monkeypatch.setattr(app, "_request_absolute_cursor_position", lambda: None)
                    redraw = app._redraw
                    if failure == "repaint":
                        monkeypatch.setattr(
                            app, "_redraw", Mock(side_effect=RuntimeError("repaint failed"))
                        )
                    task = asyncio.create_task(handoff())
                    if not known_position:
                        await waiting.wait()
                        check_preserved()
                        assert app._running_in_terminal
                        if failure == "cancel":
                            task.cancel()
                        else:
                            release.set()
                    if failure == "cancel":
                        with pytest.raises(asyncio.CancelledError):
                            await task
                    elif failure:
                        with pytest.raises(RuntimeError, match=f"{failure} failed"):
                            await task
                    else:
                        await task
                    assert not app._running_in_terminal
                    if failure == "repaint":
                        # The renderer never reacquired paste; scope cleanup must
                        # restore even though reset already forgot ownership.
                        assert stream.getvalue().count(disable) == 1
                        monkeypatch.setattr(app, "_redraw", redraw)
                    else:
                        check_preserved()
                    app.exit()
                except BaseException as exc:
                    app.exit(exception=exc)

            await asyncio.wait_for(
                app.run_async(pre_run=lambda: app.create_background_task(exercise())), 3
            )
            text = stream.getvalue()
            assert text.count(enable) == text.count(disable) == 1

    asyncio.run(run())


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_preserve_keyboard_scope_releases_without_repaint(failure):
    stream = TerminalStream()
    output = keyboard_output(terminal_output(stream))
    output.enable_bracketed_paste()
    with pytest.raises(failure):
        with output.preserve_keyboard():
            with output.preserve_keyboard():
                output.disable_bracketed_paste()
            output.flush()
            assert KEYBOARD_POP not in stream.getvalue()
            raise failure
    assert stream.getvalue().count(KEYBOARD_PUSH) == stream.getvalue().count(KEYBOARD_POP) == 1
    # Preservation is scoped: a later ordinary handoff must restore normally.
    output.enable_bracketed_paste()
    output.disable_bracketed_paste()
    output.flush()
    assert stream.getvalue().count(KEYBOARD_POP) == 2


def test_preserve_keyboard_does_not_pop_on_wrong_screen():
    stream = TerminalStream()
    output = keyboard_output(terminal_output(stream))
    output.enter_alternate_screen()
    output.enable_bracketed_paste()
    with output.preserve_keyboard():
        output.quit_alternate_screen()
        output.disable_bracketed_paste()
        output.enable_bracketed_paste()
    output.disable_bracketed_paste()
    output.flush()
    text = stream.getvalue()
    assert text.count(KEYBOARD_PUSH) == text.count(KEYBOARD_POP) == 2
    assert text.index(KEYBOARD_POP) < text.index("\x1b[?1049l") < text.rindex(KEYBOARD_PUSH)


@pytest.mark.parametrize("vi_mode", [False, True])
@pytest.mark.parametrize("enter", ["\x1b[13;5u", "\x1b[27;5;13~"])
def test_encoded_ctrl_enter_uses_interrupt_callback(vi_mode, enter):
    async def run():
        with create_pipe_input() as pipe:
            sent = []

            def interrupt(text):
                sent.append(text)
                prompt.app.exit()

            prompt = create_prompt(
                CommandRegistry(),
                input=pipe,
                output=DummyOutput(),
                vi_mode=vi_mode,
                transcript=Transcript(Console(file=StringIO())),
                on_submit=lambda text: pytest.fail("ordinary submit was called"),
                on_interrupt_submit=interrupt,
            )
            pipe.send_text("one\x1b[13;2utwo" + enter)
            await asyncio.wait_for(prompt.app.run_async(), 3)
            assert sent == ["one\ntwo"]

    asyncio.run(run())
