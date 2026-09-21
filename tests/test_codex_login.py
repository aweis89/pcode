import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from pcode import codex_login
from pcode.auth import LoginError


def make_app(model: str):
    from pcode.app import PreviewApp

    runtime = SimpleNamespace(agent=SimpleNamespace(model="original"), history=["existing"])
    buffer = StringIO()
    app = PreviewApp(model=model, runtime=runtime, console=Console(file=buffer))
    return app, runtime, buffer


def test_login_command_rebuilds_a_codex_conversation_model(monkeypatch):
    app, runtime, buffer = make_app("openai-codex:test-model")
    calls = []

    async def fake_login(**kwargs):
        kwargs["notify"]("https://auth.openai.com/oauth/authorize?state=synthetic")
        calls.append("login")

    monkeypatch.setattr(codex_login, "login", fake_login)
    monkeypatch.setattr("pcode.agent.codex_model", lambda model: f"rebuilt {model}")
    app.handle("/login openai-codex")
    assert app.login_requested == "openai-codex"
    asyncio.run(app.perform_login())

    assert app.login_requested is None
    assert calls == ["login"]
    assert runtime.agent.model == "rebuilt openai-codex:test-model"
    assert runtime.history == ["existing"]
    output = buffer.getvalue()
    assert "auth.openai.com" in output and "Signed in to OpenAI Codex" in output


def test_login_keeps_a_non_codex_conversation_model(monkeypatch):
    app, runtime, _ = make_app("anthropic:test-model")

    async def fake_login(**kwargs):
        pass

    monkeypatch.setattr(codex_login, "login", fake_login)
    app.handle("/login openai-codex")
    asyncio.run(app.perform_login())
    assert runtime.agent.model == "original"


def test_failed_login_reports_without_touching_the_model(monkeypatch):
    app, runtime, buffer = make_app("openai-codex:test-model")

    async def fail(**kwargs):
        raise LoginError("The `codex` command was not found.")

    monkeypatch.setattr(codex_login, "login", fail)
    app.handle("/login openai-codex")
    asyncio.run(app.perform_login())
    assert runtime.agent.model == "original"
    assert "`codex` command was not found" in buffer.getvalue()


def test_unknown_login_source_shows_usage():
    app, _, buffer = make_app("openai-codex:test-model")
    app.handle("/login gemini")
    assert app.login_requested is None
    assert "Usage: /login [anthropic|openai-codex]" in buffer.getvalue()


def test_logout_preserves_cli_store(tmp_path, monkeypatch):
    cli = tmp_path / "cli" / "auth.json"
    cli.parent.mkdir()
    cli.write_text("synthetic-cli")
    monkeypatch.setenv("CODEX_HOME", str(cli.parent))
    codex_login.write_credentials(codex_login.credentials_path(), tokens())
    app, _, buffer = make_app("openai-codex:test-model")
    app.handle("/logout openai-codex")
    asyncio.run(app.perform_logout())
    assert not codex_login.have_credentials()
    assert cli.read_text() == "synthetic-cli"
    assert "Removed pcode's stored" in buffer.getvalue()


def tokens():
    from pydantic_ai.providers.openai_codex import OpenAICodexCredentials

    return OpenAICodexCredentials(
        access_token="synthetic-access", refresh_token="synthetic-refresh", account_id="account"
    )


def test_store_roundtrip_and_permissions(tmp_path):
    path = tmp_path / "private" / "credentials.json"
    store = codex_login.CredentialStore(path)
    asyncio.run(store.save(tokens()))
    assert asyncio.run(store.load()) == tokens()
    assert path.stat().st_mode & 0o777 == 0o600
    assert codex_login.credential_source(path) is not None
    assert codex_login.delete_credentials(path)
    assert codex_login.credential_source(path) is None


@pytest.mark.parametrize("contents", ["invalid", "{}", '{"openai-codex": {"type": "oauth"}}'])
def test_invalid_store_fails_closed(tmp_path, contents):
    path = tmp_path / "credentials.json"
    path.write_text(contents)
    assert codex_login.credential_source(path) is not None
    with pytest.raises(LoginError, match="No usable pcode"):
        codex_login.read_credentials(path)


def test_config_directory_precedence(monkeypatch, tmp_path):
    from pcode.anthropic_oauth import credentials_path as anthropic_path
    from pcode.preferences import preferences_path

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert codex_login.credentials_path() == tmp_path / "xdg/pcode/codex-credentials.json"
    monkeypatch.setenv("PCODE_CONFIG_DIR", str(tmp_path / "override"))
    for path in (preferences_path(), anthropic_path(), codex_login.credentials_path()):
        assert path.parent == tmp_path / "override"


def test_login_uses_flow_and_persists(tmp_path):
    class Flow:
        def authorization_url(self):
            return "https://example.test/authorize"

        async def exchange_code_from_callback(self):
            return tokens()

    path = tmp_path / "credentials.json"
    urls = []
    result = asyncio.run(
        codex_login.login(path=path, flow=Flow(), open_browser=False, notify=urls.append)
    )
    assert result == codex_login.read_credentials(path) == tokens()
    assert urls == ["https://example.test/authorize"]


def test_login_timeout_preserves_old_store(tmp_path):
    class Flow:
        def authorization_url(self):
            return "https://example.test/authorize"

        async def exchange_code_from_callback(self):
            await asyncio.sleep(10)

    path = tmp_path / "credentials.json"
    codex_login.write_credentials(path, tokens())
    with pytest.raises(LoginError, match="timed out"):
        asyncio.run(codex_login.login(path=path, flow=Flow(), open_browser=False, timeout=0.01))
    assert codex_login.read_credentials(path) == tokens()


@pytest.mark.parametrize("proxy", [False, True])
def test_model_prefers_pcode_store_without_cli(monkeypatch, tmp_path, proxy):
    from pcode.agent import codex_model
    from pcode.models import active_providers

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-cli"))
    if proxy:
        monkeypatch.setenv("PCODE_LLM_PROXY", "http://localhost:9999")
    else:
        monkeypatch.delenv("PCODE_LLM_PROXY", raising=False)
    codex_login.write_credentials(codex_login.credentials_path(), tokens())
    model = codex_model("openai-codex:test-model")
    assert isinstance(model.provider._credential_source, codex_login.CredentialStore)
    assert "openai-codex" in active_providers(None)


def test_provider_refresh_is_saved_for_next_launch(monkeypatch):
    from pydantic_ai.providers import openai_codex

    store = codex_login.CredentialStore()
    refreshed = openai_codex.OpenAICodexCredentials(
        access_token="rotated-access", refresh_token="rotated-refresh", account_id="account"
    )

    async def refresh(credentials, **kwargs):
        assert credentials == tokens()
        return refreshed

    monkeypatch.setattr(openai_codex, "_refresh_credentials", refresh)

    async def run():
        await store.save(tokens())
        provider = openai_codex.OpenAICodexProvider(credential_source=store)
        async with provider:
            await provider._load_if_needed()
            async with provider._refresh_lock:
                await provider._refresh_locked()
        next_provider = openai_codex.OpenAICodexProvider(credential_source=store)
        async with next_provider:
            await next_provider._load_if_needed()
            assert next_provider.credentials == refreshed

    asyncio.run(run())


def test_cancelled_login_does_not_replace_credentials(tmp_path):
    class Flow:
        def authorization_url(self):
            return "https://example.test/authorize"

        async def exchange_code_from_callback(self):
            raise asyncio.CancelledError

    path = tmp_path / "credentials.json"
    codex_login.write_credentials(path, tokens())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(codex_login.login(path=path, flow=Flow(), open_browser=False))
    assert codex_login.read_credentials(path) == tokens()
