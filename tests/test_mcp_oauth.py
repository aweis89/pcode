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

from pcode.mcp import MCPState, build_toolset, config_path, mcp_transport

URL = "https://resource.example/mcp"
ISSUER = "https://auth.example"


def test_native_oauth_is_constructed_without_network_or_browser(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Building a toolset must not authenticate")

    monkeypatch.setattr("webbrowser.open", unexpected)
    monkeypatch.setattr(httpx2.AsyncClient, "send", unexpected)
    with warnings.catch_warnings(record=True) as caught:
        toolset = build_toolset("remote", {"url": URL, "auth": "oauth"})
    auth = mcp_transport(toolset).auth
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
    assert mcp_transport(toolset).headers == {"X-Tenant": "test"}


def test_activation_does_not_persist_tokens_or_read_disabled_credentials(monkeypatch):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"remote": {"url": URL, "auth": "oauth"}}}))
    before = path.read_bytes()
    providers = []

    def configured_toolset(name, raw):
        toolset = build_toolset(name, raw)
        provider = FakeMCPOAuthProvider()
        provider.install_toolset(toolset)
        providers.append(provider)
        return toolset

    monkeypatch.setattr("pcode.mcp.build_toolset", configured_toolset)

    async def run():
        state = MCPState()
        assert state.toolsets() == []
        await state.enable("remote")
        first = mcp_transport(state.enabled["remote"]).auth
        assert providers[0].browser_visits == 1
        await state.enable("remote")
        assert len(providers) == 1
        assert mcp_transport(state.enabled["remote"]).auth is first
        # Ordinary subsequent MCP connections use the already authenticated client.
        async with state.enabled["remote"]:
            pass
        assert providers[0].browser_visits == 1
        state.disable("remote")
        assert state.toolsets() == []
        await state.enable("remote")
        assert mcp_transport(state.enabled["remote"]).auth is not first
        assert providers[1].browser_visits == 1
        assert path.read_bytes() == before
        assert list(path.parent.iterdir()) == [path]

    asyncio.run(run())


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
        auth = mcp_transport(toolset).auth
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
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
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
        assert auth._callback_socket is None

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_native_loopback_callback_closes_listener(cancel):
    """Exercise FastMCP's actual callback server, not a mock browser or real account."""

    async def run():
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
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


def test_callback_port_collision_is_replaced_before_registration():
    """Reproduce a port being taken after /mcp enable, before OAuth starts."""
    import signal
    import socket

    async def run():
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
        provider = FakeOAuthProvider()
        browser_task = None
        original_signals = [signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)]
        registered_redirects = []

        async def http(request):
            if request.url.path == "/register":
                redirect = json.loads(request.content)["redirect_uris"][0]
                registered_redirects.append(redirect)
                # This must be an owned listening socket, not a free-port probe.
                with socket.socket() as competing:
                    competing.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    with pytest.raises(OSError):
                        competing.bind(("127.0.0.1", urlsplit(redirect).port))
            return await provider.http(request)

        async def browser_callback():
            redirect = provider.authorization["redirect_uri"][0]
            assert registered_redirects == [redirect]
            assert urlsplit(redirect).hostname == "127.0.0.1"
            async with httpx2.AsyncClient(trust_env=False, timeout=5) as client:
                response = await client.get(
                    redirect,
                    params={
                        "code": "fake-authorization-code",
                        "state": provider.authorization["state"][0],
                    },
                )
                assert response.status_code == 200

        async def redirect(url):
            nonlocal browser_task
            await provider.redirect(url)
            browser_task = asyncio.create_task(browser_callback())

        auth.context.redirect_handler = redirect
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            old_port = occupied.getsockname()[1]
            auth.redirect_port = old_port
            try:
                async with httpx2.AsyncClient(
                    auth=auth, transport=httpx2.MockTransport(http)
                ) as client:
                    assert (await client.get(URL)).status_code == 200
                await browser_task
                assert auth.redirect_port != old_port
                assert occupied.fileno() != -1  # Never close someone else's listener.
                assert auth._callback_socket is None
                with socket.socket() as reusable:
                    reusable.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    reusable.bind(("127.0.0.1", auth.redirect_port))
                assert original_signals == [
                    signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)
                ]
            finally:
                if browser_task is not None and not browser_task.done():
                    browser_task.cancel()
                    await asyncio.gather(browser_task, return_exceptions=True)

    asyncio.run(run())


def test_registered_port_collision_is_recoverable_without_browser():
    import socket

    async def run():
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
        provider = FakeOAuthProvider()
        async with httpx2.AsyncClient(auth=auth, transport=provider.install(auth)) as client:
            assert (await client.get(URL)).status_code == 200
        # Model a server requiring a new authorization while the original redirect
        # is registered. Silently changing its port would violate that registration.
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(("127.0.0.1", auth.redirect_port))
            occupied.listen()
            with pytest.raises(RuntimeError, match="Disable and re-enable"):
                await auth._reserve_callback()
        assert provider.browser_visits == 1
        assert auth._callback_socket is None

    asyncio.run(run())


async def _exercise_callback_startup_failure():
    """Run in a child process: a regressed SystemExit must not kill pytest itself."""
    from unittest.mock import patch

    from pcode.diagnostics import error_details

    toolset = build_toolset("remote", {"url": URL, "auth": "oauth"})
    transport = mcp_transport(toolset)
    auth = transport.auth
    provider = FakeOAuthProvider()
    auth.context.redirect_handler = provider.redirect
    transport.httpx_client_factory = lambda **kwargs: httpx2.AsyncClient(
        transport=httpx2.MockTransport(provider.http), **kwargs
    )

    async def fail_startup(self, sockets=None):
        raise SystemExit(3)

    loop_errors = []
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, context: loop_errors.append(context)
    )
    with patch("uvicorn.Server.serve", fail_startup):
        try:
            async with toolset:
                raise AssertionError("Broken callback server unexpectedly connected")
        except Exception as error:
            assert "callback server could not start" in str(error_details(error))
        else:
            raise AssertionError("Expected a normal, recoverable exception")
    await asyncio.sleep(0)
    assert auth._callback_socket is None
    assert not loop_errors
    print("callback failure handled without terminating pcode")


def test_embedded_callback_systemexit_does_not_kill_mcp_client():
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import asyncio, runpy; "
        f"ns = runpy.run_path({str(Path(__file__).resolve())!r}); "
        "asyncio.run(ns['_exercise_callback_startup_failure']())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "callback failure handled" in result.stdout
    assert "unhandled exception during asyncio.run() shutdown" not in result.stderr


def test_callback_timeout_releases_reserved_port():
    import socket

    from pcode.diagnostics import error_details

    async def run():
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
        auth._callback_timeout = 0.05
        with pytest.raises(Exception) as error:
            await auth.callback_handler()
        assert error_details(error.value)["type"] == "TimeoutError"
        assert auth._callback_socket is None
        with socket.socket() as reusable:
            reusable.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            reusable.bind(("127.0.0.1", auth.redirect_port))

    asyncio.run(run())


class FakeMCPOAuthProvider(FakeOAuthProvider):
    """Fake OAuth provider plus the real MCP HTTP initialization exchange."""

    async def http(self, request):
        if (
            str(request.url) == URL
            and request.headers.get("Authorization") == f"Bearer {self.access_token}"
        ):
            if request.method != "POST":
                return httpx2.Response(405)
            message = json.loads(request.content)
            if "id" not in message:
                return httpx2.Response(202)
            if message["method"] == "initialize":
                result = {
                    "protocolVersion": message["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-oauth", "version": "1"},
                }
            elif message["method"] == "tools/list":
                result = {"tools": []}
            else:
                result = {}
            return httpx2.Response(
                200, json={"jsonrpc": "2.0", "id": message["id"], "result": result}
            )
        return await super().http(request)

    def install_toolset(self, toolset):
        transport = mcp_transport(toolset)
        self.install(transport.auth)
        transport.httpx_client_factory = lambda **kwargs: httpx2.AsyncClient(
            transport=httpx2.MockTransport(self.http), **kwargs
        )


@pytest.mark.parametrize("mode", ["denied", "bad-state", "wait"])
def test_enable_oauth_failure_or_cancel_leaves_server_off(monkeypatch, mode):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"remote": {"url": URL, "auth": "oauth"}}}))

    async def run():
        state = MCPState()
        toolset = build_toolset("remote", {"url": URL, "auth": "oauth"})
        provider = FakeMCPOAuthProvider()
        provider.callback_mode = mode
        provider.install_toolset(toolset)
        monkeypatch.setattr("pcode.mcp.build_toolset", lambda name, raw: toolset)
        if mode == "wait":
            task = asyncio.create_task(state.enable("remote"))
            try:
                await asyncio.wait_for(provider.callback_started.wait(), 5)
                assert state.toolsets() == []
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        else:
            with pytest.raises(Exception):
                await state.enable("remote")
        assert state.toolsets() == []
        assert mcp_transport(toolset).auth._callback_socket is None
        # Retry after failure/cancellation should succeed, without poisoning the client.
        provider.callback_mode = "success"
        await state.enable("remote")
        assert state.toolsets() == [toolset]

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_enable_publishes_only_after_connection_teardown(monkeypatch, outcome):
    from types import SimpleNamespace

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"remote": {"url": URL, "auth": "oauth"}}}))

    async def run():
        exiting = asyncio.Event()
        finish = asyncio.Event()

        class Connection:
            wrapped = SimpleNamespace(
                client=SimpleNamespace(transport=SimpleNamespace(auth=object()))
            )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                exiting.set()
                await finish.wait()
                if outcome == "failure":
                    raise RuntimeError("Connection teardown failed")

        connection = Connection()
        monkeypatch.setattr("pcode.mcp.build_toolset", lambda name, raw: connection)
        state = MCPState()
        task = asyncio.create_task(state.enable("remote"))
        try:
            await asyncio.wait_for(exiting.wait(), 5)
            assert state.toolsets() == []
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome == "failure":
                finish.set()
                with pytest.raises(RuntimeError, match="teardown failed"):
                    await task
            else:
                finish.set()
                await task
            assert state.toolsets() == ([connection] if outcome == "success" else [])
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
