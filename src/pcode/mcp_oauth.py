"""Keep native OAuth, but own the loopback socket instead of probing a free port.

FastMCP 4.0.4 probes/closes a port at construction, then Uvicorn binds it much
later. A collision raises SystemExit inside its background task. Reserve the
actual socket before registration and pass it to an embedded, signal-free server.
"""

import asyncio
import socket
from contextlib import aclosing, nullcontext

import anyio
from fastmcp.client.auth import OAuth
from fastmcp.client.oauth_callback import OAuthCallbackResult, create_oauth_callback_server
from mcp.shared.auth import AuthorizationCodeResult
from pydantic import AnyHttpUrl
from uvicorn import Server

_HOST = "127.0.0.1"


class _CallbackServer(Server):
    def capture_signals(self):
        # This is a component of pcode, not a process-wide web server. Ctrl+C must
        # keep reaching pcode's cancellation handler, even with multiple MCP clients.
        return nullcontext()

    async def serve(self, sockets=None):
        try:
            await super().serve(sockets=sockets)
        except SystemExit:
            # Catch inside the spawned task: catching outside a TaskGroup is too
            # late to stop asyncio treating SystemExit as process termination.
            raise RuntimeError("MCP OAuth callback server could not start.") from None
        finally:
            for listener in getattr(self, "servers", []):
                listener.close()
                await listener.wait_closed()


class LoopbackOAuth(OAuth):
    """FastMCP handles protocol/security; this adapter owns callback I/O lifetime."""

    def __init__(self):
        super().__init__(client_name="pcode", callback_host=_HOST)
        self._callback_socket: socket.socket | None = None
        self._flow_lock = asyncio.Lock()

    async def _reserve_callback(self) -> socket.socket:
        if self._callback_socket is not None:
            return self._callback_socket
        registered = await self.token_storage_adapter.get_client_info()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((_HOST, self.redirect_port))
            except OSError:
                if registered is not None:
                    # A registered redirect URI cannot be silently changed. Let
                    # the user discard the in-memory registration and try again.
                    raise RuntimeError(
                        "MCP OAuth callback port is unavailable. Disable and re-enable "
                        "this MCP server, then retry sign-in."
                    ) from None
                sock.bind((_HOST, 0))
            sock.listen()
            sock.setblocking(False)
            self.redirect_port = sock.getsockname()[1]
            self.context.client_metadata.redirect_uris = [
                AnyHttpUrl(f"http://{_HOST}:{self.redirect_port}/callback")
            ]
            self._callback_socket = sock
            return sock
        except BaseException:
            sock.close()
            raise

    def _close_callback(self) -> None:
        if self._callback_socket is not None:
            self._callback_socket.close()
            self._callback_socket = None

    async def async_auth_flow(self, request):
        # The SDK already serializes its protocol flow. Include our reservation
        # in that lifetime too, so concurrent requests cannot change redirect URIs.
        async with self._flow_lock:
            try:
                if await self.token_storage_adapter.get_tokens() is None:
                    await self._reserve_callback()
                async with aclosing(super().async_auth_flow(request)) as flow:
                    try:
                        outgoing = await anext(flow)
                        while True:
                            response = yield outgoing
                            outgoing = await flow.asend(response)
                    except StopAsyncIteration:
                        return
            finally:
                self._close_callback()

    async def redirect_handler(self, authorization_url: str) -> None:
        # Refresh can fail and require a new login even when the flow began with
        # tokens. Verify socket ownership before opening a browser in that case.
        await self._reserve_callback()
        await super().redirect_handler(authorization_url)

    async def callback_handler(self) -> AuthorizationCodeResult:
        sock = await self._reserve_callback()
        try:
            result = OAuthCallbackResult()
            ready = anyio.Event()
            stopped = anyio.Event()
            server = _CallbackServer(
                create_oauth_callback_server(
                    port=self.redirect_port,
                    host=_HOST,
                    server_url=self.mcp_url,
                    result_container=result,
                    result_ready=ready,
                ).config
            )
            server.config.timeout_graceful_shutdown = 1

            async def serve():
                try:
                    await server.serve(sockets=[sock])
                finally:
                    stopped.set()

            async with anyio.create_task_group() as group:
                group.start_soon(serve)
                try:
                    with anyio.fail_after(self._callback_timeout):
                        await ready.wait()
                    if result.error:
                        raise result.error
                    return AuthorizationCodeResult(
                        code=result.code, state=result.state, iss=result.iss
                    )
                finally:
                    server.should_exit = True
                    # Shield cleanup from the cancelled model turn. Bound the wait
                    # and explicitly close listeners even if startup/shutdown fails.
                    with anyio.move_on_after(2, shield=True):
                        await stopped.wait()
                    group.cancel_scope.cancel()
        finally:
            self._close_callback()
