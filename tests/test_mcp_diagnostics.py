"""MCP setup failures retain useful causes without model advice or credentials."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pcode.app import PreviewApp
from pcode.mcp import error_message
from pcode.sessions import SavedSession


def test_wrapped_mcp_failure_shows_redacted_cause(monkeypatch):
    monkeypatch.setenv("MCP_SECRET", "private-client-secret")
    cause = RuntimeError("OAuth registration rejected: private-client-secret")
    wrapper = RuntimeError("Client failed to connect")
    wrapper.__cause__ = ExceptionGroup("task group", [cause])
    message = error_message(wrapper)
    assert "OAuth registration rejected: [redacted]" in message
    assert "private-client-secret" not in message
    assert "Client failed to connect" in message
    assert "model string" not in message
    assert "provider credentials" not in message


def test_mcp_failure_is_bounded_and_handles_cycles():
    error = RuntimeError("x" * 5000)
    error.__cause__ = error
    message = error_message(error)
    assert len(message) < 2200
    assert "Check the MCP server" in message


def test_empty_mcp_failure_preserves_exception_type():
    assert "TimeoutError" in error_message(TimeoutError())


@pytest.mark.parametrize("storage", ["saved", "no-save", "write-failure"])
@pytest.mark.parametrize("startup", [False, True])
def test_mcp_failure_reports_redacted_frames(tmp_path, monkeypatch, storage, startup):
    monkeypatch.setenv("MCP_SECRET", "private-client-secret")
    saved = (
        SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
        if storage != "no-save"
        else None
    )
    if storage == "write-failure":
        # Existing logs must not be reported as containing a traceback when the
        # new write fails. Exercise record_error's real OSError handling.
        (saved.directory / "errors.log").mkdir()

    async def fail_initialize(*args, **kwargs):
        try:
            raise TimeoutError("Bearer private-client-secret")
        except TimeoutError as cause:
            raise RuntimeError("Failed to initialize server session") from cause

    app = PreviewApp(
        model="test:local",
        runtime=SimpleNamespace(session=saved, mcp=SimpleNamespace(enable=fail_initialize)),
    )
    app.transcript = Mock()

    async def run():
        if startup:
            await app.enable_mcp_defaults(["remote"])
        else:
            try:
                await app.enable_mcp("remote")
            except Exception as error:
                app.report_mcp_error("remote", error)

    try:
        asyncio.run(run())
        notes = "\n".join(call.args[0] for call in app.transcript.note.call_args_list)
        if storage == "saved":
            path = saved.directory / "errors.log"
            assert str(path) in notes
            report = path.read_text()
            assert "mcp:remote" in report
            assert path.stat().st_mode & 0o777 == 0o600
        else:
            report = notes
            assert "MCP diagnostics:" not in notes
            if storage == "no-save":
                assert not (tmp_path / "sessions").exists()
        assert "in fail_initialize" in report
        assert "TimeoutError" in report
        assert "Failed to initialize server session" in report
        assert "private-client-secret" not in report
        assert "[redacted]" in report
        app.transcript.error.assert_called_once()
    finally:
        if saved:
            saved.close()
