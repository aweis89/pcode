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


URL = "https://mcp.example.test/mcp"


def _failing_endpoint(status, challenge=None):
    """The real SDK and FastMCP stack against an endpoint that answers `status`."""
    import httpx2

    from pcode.mcp import mcp_transport

    def http(request):
        headers = {"WWW-Authenticate": challenge} if challenge else {}
        return httpx2.Response(status, headers=headers, text="denied")

    def install(toolset):
        mcp_transport(toolset).httpx_client_factory = lambda **kwargs: httpx2.AsyncClient(
            transport=httpx2.MockTransport(http), **kwargs
        )
        return toolset

    return install


def _connect(toolset):
    async def run():
        async with toolset:
            pass

    with pytest.raises(Exception) as caught:
        asyncio.run(run())
    return caught.value


OAUTH_CHALLENGE = f'Bearer resource_metadata="{URL}/.well-known/oauth-protected-resource"'


@pytest.mark.parametrize(
    ("entry", "status", "challenge", "hint"),
    [
        # A server that offers OAuth, configured without it.
        ({"url": URL}, 401, OAUTH_CHALLENGE, '"auth": "oauth"'),
        # A wrong API key: the header, not OAuth, is what to fix.
        (
            {"url": URL, "headers": {"Authorization": "Token token=wrong"}},
            401,
            OAUTH_CHALLENGE,
            "credentials in the headers",
        ),
        ({"url": URL}, 401, None, "add an Authorization header"),
        ({"url": URL}, 502, None, "try again later"),
    ],
)
def test_http_failure_names_the_server_status_and_fix(entry, status, challenge, hint):
    from pcode.live import error_message as turn_error
    from pcode.mcp import MCPConnectError, build_toolset, config_path

    toolset = _failing_endpoint(status, challenge)(build_toolset("remote", entry))
    error = _connect(toolset)
    assert isinstance(error, MCPConnectError)
    message = turn_error(error)
    assert f"MCP server 'remote' failed to connect: HTTP {status}" in message
    assert hint in message
    if status == 401:
        # Config is captured at enable, so the fix names the file and the reload.
        assert str(config_path()) in message
        assert "`/mcp disable remote` and `/mcp enable remote`" in message
    # Nothing the server sent, and no credential from the config.
    assert "denied" not in message and "wrong" not in message


def test_failure_without_a_response_still_names_the_server():
    import httpx2

    from pcode.live import error_message as turn_error
    from pcode.mcp import build_toolset, mcp_transport

    def refuse(request):
        raise httpx2.ConnectError("connection refused", request=request)

    toolset = build_toolset("remote", {"url": URL})
    mcp_transport(toolset).httpx_client_factory = lambda **kwargs: httpx2.AsyncClient(
        transport=httpx2.MockTransport(refuse), **kwargs
    )
    message = turn_error(_connect(toolset))
    assert "MCP server 'remote' failed to connect (" in message
    assert "HTTP" not in message
