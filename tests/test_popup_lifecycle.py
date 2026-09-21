"""All alternate-screen dialogs share terminal ownership and restoration."""

import asyncio
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rich.console import Console

from pcode.app import PreviewApp


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

        async def flush():
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
