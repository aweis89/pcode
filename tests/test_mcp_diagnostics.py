"""MCP setup failures retain useful causes without model advice or credentials."""

from pcode.mcp import error_message


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
