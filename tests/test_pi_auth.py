"""All credentials and HTTP responses in these tests are synthetic."""

import asyncio
import json
import time
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from rich.console import Console

from pcode import pi_auth
from pcode.auth import LoginError
from pcode.pi_auth import OAUTH_BETAS, OAUTH_PREAMBLE, PiAnthropicModel, read_pi_credential


def entry(kind="oauth", value="synthetic-access"):
    if kind == "api_key":
        return {"type": kind, "key": value}
    return {
        "type": kind,
        "access": value,
        "refresh": "synthetic-refresh-never-used",
        "expires": (time.time() + 3600) * 1000,
    }


@pytest.fixture
def pi_file(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-pi.json"
    path.write_text(json.dumps({"anthropic": entry()}))
    monkeypatch.setattr(pi_auth, "pi_auth_path", lambda: path)
    monkeypatch.setenv("PCODE_ANTHROPIC_AUTH", "api-key")
    monkeypatch.delenv("PCODE_LLM_PROXY", raising=False)
    return path


def test_load_is_read_only_and_repr_hides_token(pi_file):
    before = pi_file.read_bytes()
    credential = read_pi_credential(pi_file)
    assert credential.kind == "oauth"
    assert credential.value == "synthetic-access"
    assert "synthetic-access" not in repr(credential)
    assert pi_file.read_bytes() == before


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        {"anthropic": []},
        {"anthropic": {"type": "unknown", "key": "synthetic"}},
        {"anthropic": {"type": "api_key", "key": "!do-not-execute"}},
        {"anthropic": {"type": "api_key", "key": "$DO_NOT_RESOLVE"}},
        {"anthropic": {"type": "api_key", "key": "line\nbreak"}},
        {"anthropic": {**entry(), "expires": True}},
        {"anthropic": {**entry(), "expires": float("nan")}},
        {"anthropic": {**entry(), "expires": "not-a-number"}},
        {"anthropic": {**entry(), "access": ""}},
    ],
)
def test_invalid_entries_are_sanitized(pi_file, data):
    pi_file.write_text(json.dumps(data))
    with pytest.raises(LoginError, match="Cannot load pi's Anthropic credential") as error:
        read_pi_credential(pi_file)
    assert "synthetic" not in str(error.value)


def test_malformed_and_missing_file(pi_file):
    pi_file.write_text('malformed "synthetic-secret"')
    with pytest.raises(LoginError) as error:
        read_pi_credential(pi_file)
    assert "synthetic-secret" not in str(error.value)
    pi_file.unlink()
    with pytest.raises(LoginError, match="Log in to Anthropic in pi first"):
        read_pi_credential(pi_file)


def test_expiry_requires_pi_refresh(pi_file):
    pi_file.write_text(json.dumps({"anthropic": {**entry(), "expires": 0}}))
    before = pi_file.read_bytes()
    with pytest.raises(LoginError, match="Refresh it in pi"):
        read_pi_credential(pi_file)
    assert pi_file.read_bytes() == before


def test_custom_pi_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    assert pi_auth.pi_auth_path() == tmp_path / "auth.json"


@pytest.mark.parametrize("kind", ["oauth", "api_key"])
def test_wire_auth_and_rotation(pi_file, monkeypatch, kind):
    # Other auth and endpoint variables must not redirect or override pi auth.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-unrelated-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-unrelated-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unrelated.invalid")
    requests = []

    def handle(request):
        requests.append(request)
        return httpx2.Response(400, json={"error": {"message": "end of mock request"}})

    async def run():
        pi_file.write_text(json.dumps({"anthropic": entry(kind)}))
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            model = PiAnthropicModel("anthropic:test-model", http_client=client)
            agent = Agent(model, instructions="Keep original instructions.")
            for value in ["synthetic-access", "synthetic-rotated"]:
                pi_file.write_text(json.dumps({"anthropic": entry(kind, value)}))
                before = pi_file.read_bytes()
                with pytest.raises(ModelHTTPError):
                    await agent.run("hello")
                assert pi_file.read_bytes() == before
                request = requests[-1]
                assert request.url.host == "api.anthropic.com"
                headers = request.headers
                if kind == "oauth":
                    assert headers["authorization"] == f"Bearer {value}"
                    assert "x-api-key" not in headers
                    assert OAUTH_BETAS <= set(headers["anthropic-beta"].split(","))
                    assert headers["x-app"] == "cli"
                    assert headers["user-agent"] == pi_auth.OAUTH_USER_AGENT
                else:
                    assert headers["x-api-key"] == value
                    assert "authorization" not in headers
                    assert "oauth-2025-04-20" not in headers.get("anthropic-beta", "")
                payload = json.loads(request.content)
                assert payload["model"] == "test-model"
                assert "Keep original instructions." in json.dumps(payload["system"])
                if kind == "oauth":
                    assert payload["system"][0]["text"] == OAUTH_PREAMBLE
                else:
                    assert OAUTH_PREAMBLE not in json.dumps(payload)
                assert "synthetic" not in json.dumps(payload)

            # Changing credential type requires rebuilding the compatibility transport.
            other = "api_key" if kind == "oauth" else "oauth"
            pi_file.write_text(json.dumps({"anthropic": entry(other)}))
            with pytest.raises(LoginError, match="credential type changed"):
                await agent.run("hello")
            assert len(requests) == 2

    asyncio.run(run())


def test_explicit_source_bypasses_environment_key(pi_file, monkeypatch, tmp_path):
    from pcode.agent import create_agent

    monkeypatch.setenv("PCODE_ANTHROPIC_AUTH", "pi")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-unrelated-key")
    load = Mock(side_effect=AssertionError("must not use environment key"))
    monkeypatch.setattr("pcode.auth.anthropic_model", load)
    agent = create_agent("anthropic:test-model", tmp_path)
    assert isinstance(agent.model, PiAnthropicModel)
    load.assert_not_called()
    pi_file.unlink()
    with pytest.raises(LoginError):
        create_agent("anthropic:test-model", tmp_path)
    load.assert_not_called()


def test_normal_auth_does_not_read_pi(monkeypatch, tmp_path):
    from pcode.agent import create_agent

    monkeypatch.delenv("PCODE_LLM_PROXY", raising=False)
    monkeypatch.setenv("PCODE_ANTHROPIC_AUTH", "api-key")
    load = Mock(side_effect=AssertionError("must not read pi"))
    monkeypatch.setattr(pi_auth, "read_pi_credential", load)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    create_agent("anthropic:test-model", tmp_path)
    load.assert_not_called()


@pytest.mark.parametrize("command", ["/login", "/login pi"])
def test_login_pi_preserves_runtime_and_handles_failure(pi_file, command):
    from pcode.app import PreviewApp

    runtime = SimpleNamespace(agent=SimpleNamespace(model="original"), history=["existing"])
    buffer = StringIO()
    app = PreviewApp(model="anthropic:test-model", runtime=runtime, console=Console(file=buffer))
    app.handle(command)
    assert app.login_requested is True
    asyncio.run(app.login_pi())
    assert not app.login_requested
    assert isinstance(runtime.agent.model, PiAnthropicModel)
    assert runtime.history == ["existing"]
    assert "read-only" in buffer.getvalue()
    assert "synthetic" not in buffer.getvalue()
    original = runtime.agent.model
    pi_file.unlink()
    app.handle(command)
    asyncio.run(app.login_pi())
    assert runtime.agent.model is original
    assert "Cannot load pi's Anthropic credential" in buffer.getvalue()


def test_expiry_during_session_blocks_request_and_gives_guidance(pi_file):
    from pcode.live import error_message

    requests = []

    def handle(request):
        requests.append(request)
        return httpx2.Response(400)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            model = PiAnthropicModel("anthropic:test-model", http_client=client)
            pi_file.write_text(json.dumps({"anthropic": {**entry(), "expires": 0}}))
            with pytest.raises(LoginError) as error:
                await Agent(model).run("hello")
            assert "Refresh it in pi" in error_message(error.value)
            assert requests == []

    asyncio.run(run())
