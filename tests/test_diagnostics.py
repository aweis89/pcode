import asyncio
import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.models.function import FunctionModel

from pcode.diagnostics import error_details
from pcode.live import AgentRuntime
from pcode.sessions import SavedSession


def test_connection_cause_is_saved_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "sensitive-environment-value")

    async def model(messages, info):
        try:
            try:
                raise OSError(
                    "Connection refused: http://proxy-user:proxy-pass@localhost:8080 "
                    "Bearer sensitive-environment-value; refresh_token=hidden"
                )
            except OSError as error:
                raise ConnectionError("Proxy connection failed") from error
        except ConnectionError as error:
            raise ModelAPIError("test:local", "Connection error.") from error
        yield "unreachable"

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)), saved)

    async def run():
        with pytest.raises(ModelAPIError):
            _ = [event async for event in runtime.stream("hello")]

    try:
        asyncio.run(run())
        records = [
            json.loads(line)
            for line in (saved.directory / "transcript.jsonl").read_text().splitlines()
        ]
        detail = next(r["error"] for r in records if r["kind"] == "turn_failed")
        assert detail["type"] == "ModelAPIError"
        assert detail["message"] == "Connection error."
        assert detail["cause"]["type"] == "ConnectionError"
        assert detail["cause"]["cause"]["type"] == "OSError"
        assert "Connection refused" in detail["cause"]["cause"]["message"]
        assert "localhost:8080" in detail["cause"]["cause"]["message"]
        for secret in ("proxy-user", "proxy-pass", "sensitive-environment-value", "hidden"):
            assert secret not in json.dumps(detail)
    finally:
        runtime.close()


def test_implicit_context_and_explicit_cause_precedence():
    error = RuntimeError("wrapper")
    error.__context__ = OSError("DNS lookup failed")
    assert error_details(error)["context"]["message"] == "DNS lookup failed"
    error.__cause__ = TimeoutError("Connection timed out")
    details = error_details(error)
    assert details["cause"]["type"] == "TimeoutError"
    assert "context" not in details
    error.__cause__ = None
    assert "context" not in error_details(error)  # Equivalent to `raise ... from None`.


def test_grouped_causes_cycles_and_depth_are_bounded():
    error = RuntimeError("wrapper")
    error.__cause__ = ExceptionGroup("transport", [OSError("TLS verification failed")])
    assert error_details(error)["cause"]["message"] == "TLS verification failed"
    error.__cause__ = error
    assert error_details(error)["cause"]["truncated"] is True
    for _ in range(30):
        wrapper = RuntimeError("another wrapper")
        wrapper.__cause__ = error
        error = wrapper
    detail = error_details(error)
    for _ in range(16):
        detail = detail["cause"]
    assert detail["truncated"] is True
