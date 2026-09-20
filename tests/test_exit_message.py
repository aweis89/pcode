import asyncio
import shlex
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp


@pytest.mark.parametrize("custom_root", [False, True])
@pytest.mark.parametrize("saved", [False, True])
def test_exit_reports_active_session_continue_command(tmp_path, monkeypatch, custom_root, saved):
    default_root = tmp_path / "sessions"
    monkeypatch.setenv("PCODE_SESSION_DIR", str(default_root))
    root = tmp_path / "custom [sessions]" if custom_root else default_root
    identity = "active-session-id"
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=240))
    prompt = SimpleNamespace(app=Mock(run_async=AsyncMock(), output=DummyOutput()))

    async def exit_prompt(**kwargs):
        # The active session can change after startup (new turn or /session).
        app.runtime.session = (
            SimpleNamespace(info=SimpleNamespace(id=identity), directory=root / identity)
            if saved
            else None
        )

    prompt.app.run_async.side_effect = exit_prompt
    with patch("pcode.app.create_prompt", return_value=prompt):
        asyncio.run(app.run_async())

    output = stream.getvalue()
    assert "Goodbye." not in output
    if saved:
        command = ["pcode", "--continue", identity]
        if custom_root:
            command.extend(["--session-dir", str(root)])
        assert f"Continue with: {shlex.join(command)}" in output
    else:
        assert "Session not saved; no continue command available." in output
        assert "pcode --continue" not in output
