"""All alternate-screen dialogs share terminal ownership and restoration."""

import asyncio
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp, _PopupSuperseded
from pcode.commands import Command
from pcode.runtime import Message
from pcode.ui import TerminalOutput, create_prompt


@pytest.mark.parametrize("outcome", [None, RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("separate_input", [False, True])
def test_popup_restores_transcript_after_terminal_release(monkeypatch, outcome, separate_input):
    app = PreviewApp(console=Console(file=StringIO()))
    events = []
    editor_input = SimpleNamespace(stdin=object()) if separate_input else object()
    modal_input = Mock()
    modal_input.close.side_effect = lambda: events.append("close input")
    factory = Mock(return_value=modal_input)
    monkeypatch.setattr("pcode.app.create_input", factory)
    session = SimpleNamespace(app=SimpleNamespace(input=editor_input))

    async def run():
        lock = asyncio.Lock()

        async def flush(*, drain=False):
            assert not lock.locked()
            events.append("flush")

        @asynccontextmanager
        async def suspend(editor):
            assert editor is session.app
            assert lock.locked()
            events.append("suspend")
            try:
                yield
            finally:
                events.append("resume")

        def regenerate():
            assert not lock.locked()
            events.append("replay")

        monkeypatch.setattr("pcode.app.suspended_editor", suspend)
        monkeypatch.setattr(app.transcript, "regenerate", regenerate)
        output = SimpleNamespace(lock=lock, flush=flush)

        async def popup():
            async with app.popup(output, session) as inp:
                assert inp is (modal_input if separate_input else editor_input)
                assert lock.locked()
                events.append("dialog")
                if outcome:
                    raise outcome()

        if outcome:
            with pytest.raises(outcome):
                await popup()
        else:
            await popup()

    asyncio.run(run())
    assert events == [
        "flush",
        "suspend",
        "dialog",
        *(["close input"] if separate_input else []),
        "resume",
        "replay",
        "flush",
    ]
    if separate_input:
        factory.assert_called_once_with(stdin=editor_input.stdin)
        modal_input.close.assert_called_once_with()
    else:
        factory.assert_not_called()
        modal_input.close.assert_not_called()


def test_superseded_popup_does_not_touch_terminal(monkeypatch):
    app = PreviewApp(console=Console(file=StringIO()))
    app._popup_generation = 1
    app._command_popup_generation = 0
    output = SimpleNamespace(flush=AsyncMock())
    regenerate = Mock()
    monkeypatch.setattr(app.transcript, "regenerate", regenerate)

    async def run():
        with pytest.raises(_PopupSuperseded):
            async with app.popup(output, None):
                pytest.fail("A stale request must never open its popup")

    asyncio.run(run())
    output.flush.assert_not_called()
    regenerate.assert_not_called()
    assert app._popup_generation == 1


@pytest.mark.parametrize("timing", ["batch", "startup", "handoff"])
@pytest.mark.parametrize(
    ("first", "pending"),
    [("\x0c", "\x0c"), ("\x0c", "/status\r"), ("/status\r", "\x0c"), ("/status\r", "/status\r")],
    ids=["double-shortcut", "model-then-status", "status-then-model", "double-status"],
)
def test_opening_popup_discards_pending_popups_only(monkeypatch, tmp_path, first, pending, timing):
    async def run():
        app = PreviewApp(workspace=tmp_path, console=Console(file=StringIO()))
        initialized = asyncio.Event()
        preparing = asyncio.Event()
        prepared = asyncio.Event()
        opened = asyncio.Event()
        dismiss = asyncio.Event()
        popups = []
        commands = []
        prompts = []
        app.registry.register(Command("/marker", "Record dispatch", commands.append))

        async def initialize():
            await initialized.wait()

        def reply(text):
            prompts.append(text)
            return [Message("response")]

        async def dialog():
            popups.append("popup")
            opened.set()
            await dismiss.wait()

        async def picker(self):
            await dialog()

        monkeypatch.setattr(app, "_initialize_runtime", initialize)
        monkeypatch.setattr(app.preview, "reply", reply)
        monkeypatch.setattr("pcode.models.active_providers", lambda _: {"anthropic"})
        monkeypatch.setattr("pcode.model_ui.ModelPicker.run", picker)
        monkeypatch.setattr(
            "pcode.session_ui.session_info_dialog",
            lambda *args, **kwargs: SimpleNamespace(run_async=dialog),
        )
        if timing != "startup":
            initialized.set()
        if timing == "handoff":
            flush = TerminalOutput.flush

            async def delayed_flush(self, *, drain=False):
                if drain and not preparing.is_set():
                    preparing.set()
                    await prepared.wait()
                await flush(self, drain=drain)

            monkeypatch.setattr(TerminalOutput, "flush", delayed_flush)

        async def wait_for(predicate):
            async with asyncio.timeout(5):
                while not predicate():
                    await asyncio.sleep(0.01)

        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            monkeypatch.setattr("pcode.app.create_prompt", prompt)
            task = asyncio.create_task(app.run_async())
            try:
                await wait_for(lambda: session is not None and session.app.is_running)
                if timing == "handoff":
                    pipe.send_text(first)
                    await asyncio.wait_for(preparing.wait(), 5)
                    first_keys = ""
                else:
                    first_keys = first
                pipe.send_text(first_keys + pending + "/marker\rqueued question\rdraft\x1b[D")
                await wait_for(lambda: session.default_buffer.text == "draft")
                initialized.set()
                prepared.set()
                await asyncio.wait_for(opened.wait(), 5)
                assert commands == prompts == []
                dismiss.set()
                await wait_for(lambda: commands and prompts and not app.activity.busy)
                assert popups == ["popup"]
                assert commands == [""]
                assert prompts == ["queued question"]
                assert session.default_buffer.text == "draft"
                assert session.default_buffer.cursor_position == 4

                # A fresh shortcut after dismissal is not a pending request.
                pipe.send_text("\x0c")
                await wait_for(lambda: len(popups) == 2)
                assert session.default_buffer.text == "draft"
                pipe.send_text("\x03/quit\r")
                await asyncio.wait_for(task, 5)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
