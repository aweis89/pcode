"""Exercise native OAuth with a fake HTTP provider, never real accounts or browsers."""

import asyncio
import base64
import hashlib
import json
import warnings
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastmcp.client.auth import OAuth
from key_value.aio.stores.memory import MemoryStore
from mcp.shared.auth import AuthorizationCodeResult

from pcode.mcp import MCPState, build_toolset, config_path

URL = "https://resource.example/mcp"
ISSUER = "https://auth.example"


def test_native_oauth_is_constructed_without_network_or_browser(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Building a toolset must not authenticate")

    monkeypatch.setattr("webbrowser.open", unexpected)
    monkeypatch.setattr(httpx2.AsyncClient, "send", unexpected)
    with warnings.catch_warnings(record=True) as caught:
        toolset = build_toolset("remote", {"url": URL, "auth": "oauth"})
    auth = toolset.wrapped.client.transport.auth
    assert isinstance(auth, OAuth)
    assert isinstance(auth.token_storage_adapter._key_value_store, MemoryStore)
    assert not caught  # Storage lifetime is explained by pcode instead of a raw prompt warning.
    assert not auth._initialized
    assert toolset.prefix == "mcp_remote"


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "echo", "auth": "oauth"},
        {"url": URL, "auth": "secret-bearer-value"},
        {"url": URL, "auth": {"type": "oauth", "client_secret": "secret-bearer-value"}},
        {"url": URL, "auth": "oauth", "headers": {"Authorization": "secret-bearer-value"}},
        {"url": URL, "auth": "oauth", "headers": {"authorization": "secret-bearer-value"}},
    ],
)
def test_invalid_auth_is_rejected_without_exposing_secrets(entry):
    with pytest.raises(ValueError, match="Invalid MCP server") as error:
        build_toolset("remote", entry)
    assert "secret-bearer-value" not in str(error.value)


def test_oauth_allows_non_auth_headers():
    toolset = build_toolset(
        "remote", {"url": URL, "auth": "oauth", "headers": {"X-Tenant": "test"}}
    )
    assert toolset.wrapped.client.transport.headers == {"X-Tenant": "test"}


def test_activation_does_not_persist_tokens_or_read_disabled_credentials():
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"remote": {"url": URL, "auth": "oauth"}}}))
    before = path.read_bytes()
    state = MCPState()
    assert state.toolsets() == []
    state.enable("remote")
    first = state.enabled["remote"].wrapped.client.transport.auth
    state.enable("remote")
    assert state.enabled["remote"].wrapped.client.transport.auth is first
    state.disable("remote")
    assert state.toolsets() == []
    state.enable("remote")
    assert state.enabled["remote"].wrapped.client.transport.auth is not first
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


class FakeOAuthProvider:
    """Real SDK discovery, DCR, PKCE, exchange, and refresh over a mock transport."""

    def __init__(self):
        self.authorization = None
        self.registrations = 0
        self.grants = []
        self.browser_visits = 0
        self.access_token = "fake-access-token"
        self.callback_started = asyncio.Event()
        self.callback_mode = "success"

    async def redirect(self, url):
        assert url.startswith(ISSUER + "/authorize?")
        self.authorization = parse_qs(urlsplit(url).query)
        self.browser_visits += 1
        assert self.authorization["code_challenge_method"] == ["S256"]
        callback_url = urlsplit(self.authorization["redirect_uri"][0])
        assert callback_url.hostname in {"localhost", "127.0.0.1"}
        assert callback_url.path == "/callback"

    async def callback(self):
        self.callback_started.set()
        if self.callback_mode == "wait":
            await asyncio.Event().wait()
        if self.callback_mode == "denied":
            raise RuntimeError("User denied OAuth access")
        return AuthorizationCodeResult(
            code="fake-authorization-code",
            state="wrong-state"
            if self.callback_mode == "bad-state"
            else self.authorization["state"][0],
        )

    async def http(self, request):
        path = request.url.path
        if str(request.url) == URL:
            if request.headers.get("Authorization") != f"Bearer {self.access_token}":
                return httpx2.Response(
                    401,
                    headers={"WWW-Authenticate": f'Bearer resource_metadata="{ISSUER}/resource"'},
                )
            return httpx2.Response(200, json={"ok": True})
        if path == "/resource":
            return httpx2.Response(200, json={"resource": URL, "authorization_servers": [ISSUER]})
        if path.startswith("/.well-known/"):
            return httpx2.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/authorize",
                    "token_endpoint": ISSUER + "/token",
                    "registration_endpoint": ISSUER + "/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if path == "/register":
            self.registrations += 1
            metadata = json.loads(request.content)
            return httpx2.Response(
                201,
                json={**metadata, "client_id": "fake-client", "token_endpoint_auth_method": "none"},
            )
        if path == "/token":
            form = parse_qs(request.content.decode())
            grant = form["grant_type"][0]
            self.grants.append(grant)
            if grant == "authorization_code":
                assert form["code"] == ["fake-authorization-code"]
                challenge = (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(form["code_verifier"][0].encode()).digest()
                    )
                    .rstrip(b"=")
                    .decode()
                )
                assert self.authorization["code_challenge"] == [challenge]
            else:
                assert grant == "refresh_token"
                assert form["refresh_token"] == ["fake-refresh-token"]
                self.access_token = "fake-refreshed-access-token"
            return httpx2.Response(
                200,
                json={
                    "access_token": self.access_token,
                    "token_type": "Bearer",
                    "refresh_token": "fake-refresh-token",
                    "expires_in": 3600,
                },
            )
        pytest.fail(f"Unexpected fake provider request: {request.method} {request.url}")

    def install(self, auth):
        # Replace just browser/callback I/O. OAuth state validation and PKCE stay native.
        auth.context.redirect_handler = self.redirect
        auth.context.callback_handler = self.callback
        return httpx2.MockTransport(self.http)


def test_native_oauth_exchange_reuse_and_refresh():
    async def run():
        toolset = build_toolset("remote", {"url": URL, "auth": "oauth"})
        auth = toolset.wrapped.client.transport.auth
        provider = FakeOAuthProvider()
        transport = provider.install(auth)
        async with httpx2.AsyncClient(auth=auth, transport=transport) as client:
            response = await client.get(URL)
            assert response.status_code == 200
            assert provider.grants == ["authorization_code"]
            assert provider.browser_visits == 1
            assert provider.registrations == 1
        # A new HTTP connection reuses the enabled toolset's OAuth object.
        async with httpx2.AsyncClient(auth=auth, transport=transport) as client:
            assert (await client.get(URL)).status_code == 200
            assert provider.browser_visits == 1
            auth.context.token_expiry_time = 1
            assert (await client.get(URL)).status_code == 200
            assert provider.grants == ["authorization_code", "refresh_token"]
            assert provider.browser_visits == 1
        assert (await auth.token_storage_adapter.get_tokens()).access_token == provider.access_token

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["denied", "bad-state", "wait"])
def test_native_oauth_failure_or_cancellation_never_exchanges_a_code(mode):
    async def run():
        auth = build_toolset("remote", {"url": URL, "auth": "oauth"}).wrapped.client.transport.auth
        provider = FakeOAuthProvider()
        provider.callback_mode = mode
        async with httpx2.AsyncClient(auth=auth, transport=provider.install(auth)) as client:
            if mode == "wait":
                task = asyncio.create_task(client.get(URL))
                await asyncio.wait_for(provider.callback_started.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(Exception, match="denied|State parameter mismatch"):
                    await client.get(URL)
        assert provider.grants == []
        assert await auth.token_storage_adapter.get_tokens() is None

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_native_loopback_callback_closes_listener(cancel):
    """Exercise FastMCP's actual callback server, not a mock browser or real account."""

    async def run():
        auth = build_toolset("remote", {"url": URL, "auth": "oauth"}).wrapped.client.transport.auth
        callback_url = f"http://localhost:{auth.redirect_port}/callback"
        task = asyncio.create_task(auth.callback_handler())
        try:
            async with httpx2.AsyncClient(trust_env=False, timeout=1) as client:
                async with asyncio.timeout(5):
                    while True:
                        if task.done():
                            await task  # Surface startup errors rather than a polling timeout.
                        try:
                            response = await client.get(callback_url.replace("/callback", "/ready"))
                            if response.status_code == 404:
                                break
                        except httpx2.ConnectError:
                            pass
                        await asyncio.sleep(0.01)
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    response = await client.get(
                        callback_url, params={"code": "test-code", "state": "test-state"}
                    )
                    assert response.status_code == 200
                    result = await task
                    assert result.code == "test-code"
                    assert result.state == "test-state"
                with pytest.raises(httpx2.ConnectError):
                    await client.get(callback_url)
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(run())
