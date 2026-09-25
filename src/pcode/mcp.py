"""Explicit, conversation-scoped MCP activation; configuration alone does nothing."""

import functools
import json
import os
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from pcode.preferences import preferences_path

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def mcp_transport(toolset: Any) -> Any:
    """The client transport of the MCPToolset inside our prefix/defer wrappers."""
    while (wrapped := getattr(toolset, "wrapped", None)) is not None:
        toolset = wrapped
    return getattr(getattr(toolset, "client", None), "transport", None)


def _without_deferred_loading(toolset: Any) -> Any | None:
    """Rebuild a toolset without its deferred-loading layer, or `None` if it has none.

    Only the wrappers are rebuilt: the `MCPToolset` itself (with its client, its
    OAuth object and whatever tokens it already holds) is the same instance, so
    dropping deferral costs neither a reconnection nor a second sign-in.
    """
    from pydantic_ai.toolsets.deferred_loading import DeferredLoadingToolset

    if isinstance(toolset, DeferredLoadingToolset):
        return toolset.wrapped
    wrapped = getattr(toolset, "wrapped", None)
    if wrapped is None:
        return None
    inner = _without_deferred_loading(wrapped)
    return None if inner is None else replace(toolset, wrapped=inner)


def config_path() -> Path:
    override = os.environ.get("PCODE_MCP_CONFIG", "").strip()
    return Path(override).expanduser() if override else preferences_path().with_name("mcp.json")


def configured_servers() -> dict[str, Any]:
    """Read names without importing MCP, expanding secrets, or starting servers."""
    path = config_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        if not os.environ.get("PCODE_MCP_CONFIG", "").strip():
            return {}
        raise ValueError(f"MCP configuration not found: {path}") from None
    except (OSError, ValueError):
        raise ValueError(f"Cannot read MCP configuration: {path} (expected JSON)") from None
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
        raise ValueError("MCP configuration must contain an mcpServers object.")
    servers = data["mcpServers"]
    if any(not _NAME.fullmatch(name) for name in servers):
        raise ValueError(
            "MCP server names must start with a letter and contain only letters, digits, "
            "underscores or hyphens (up to 32 characters)."
        )
    return servers


def default_servers() -> list[str]:
    """Names marked `"enabled": true`, without validating or expanding the rest."""
    return sorted(
        name
        for name, raw in configured_servers().items()
        if isinstance(raw, dict) and raw.get("enabled") is True
    )


def _expand(value: Any) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match) -> str:
            name, default = match.groups()
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ValueError(f"Missing MCP environment variable: {name}")

        return _ENV.sub(replace, value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    auth: Literal["oauth"] | None = None
    # A client created with the provider ahead of time, for services without
    # dynamic client registration (Google's MCP servers, for one).
    client_id: str | None = None
    client_secret: str | None = None
    # Enable at startup, /new, and resume instead of waiting for /mcp enable.
    enabled: bool = False
    # Off by default: a server's schemas otherwise sit in every request of the
    # conversation, while tool search costs one call for the tools actually used.
    direct: bool = False
    # What the server is for, shown to the model beside its name in the list of
    # enabled servers. Only worth setting when the name does not already say it.
    description: str | None = None

    @model_validator(mode="after")
    def transport(self):
        if bool(self.command) == bool(self.url):
            raise ValueError("Specify exactly one of command or url.")
        if self.client_secret is not None and self.client_id is None:
            raise ValueError("client_secret needs client_id.")
        if self.client_id is not None and (self.auth != "oauth" or not self.client_id.strip()):
            raise ValueError('client_id needs auth: "oauth".')
        if self.command:
            if not self.command.strip() or any(
                item is not None
                for item in (self.headers, self.auth, self.client_id, self.client_secret)
            ):
                raise ValueError("Invalid stdio options.")
            return self
        if any(item is not None for item in (self.command, self.args, self.env, self.cwd)):
            raise ValueError("Invalid HTTP options.")
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Expected an HTTP(S) URL.")
        if self.auth and any(key.lower() == "authorization" for key in self.headers or {}):
            raise ValueError("OAuth cannot be combined with an Authorization header.")
        return self


def _config(name: str, raw: Any) -> ServerConfig:
    try:
        return ServerConfig.model_validate(_expand(raw))
    except ValidationError:
        # Pydantic errors include input values: never print credentials from config.
        raise ValueError(
            f"Invalid MCP server '{name}'. Use command/args/env/cwd for stdio or url/headers "
            'for HTTP (optional auth: "oauth" with client_id/client_secret, enabled: true, '
            "direct: true, description); "
            "other fields are not supported."
        ) from None


def build_toolset(name: str, raw: Any, *, interactive: bool = True):
    """Construct only the selected server. Connections are owned by each agent run.

    A non-interactive OAuth server may use stored tokens and refresh them, but
    fails with SignInRequired rather than opening a browser.
    """
    config = _config(name, raw)
    from fastmcp.client.transports import StdioTransport
    from pydantic_ai.mcp import MCPToolset

    try:
        if config.command:
            transport = StdioTransport(
                command=config.command,
                args=config.args or [],
                env=config.env,
                cwd=config.cwd,
                # FastMCP otherwise keeps subprocesses alive after toolset exit.
                keep_alive=False,
            )
            toolset = MCPToolset(transport, id=name)
        else:
            # FastMCP owns PKCE, browser sign-in, and refresh; pcode owns the socket
            # and the credential file (see mcp_oauth).
            from pcode.mcp_oauth import LoopbackOAuth

            auth = (
                LoopbackOAuth(
                    interactive=interactive,
                    client_id=config.client_id,
                    client_secret=config.client_secret,
                )
                if config.auth == "oauth"
                else None
            )
            toolset = MCPToolset(
                config.url,
                id=name,
                headers=config.headers,
                auth=auth,
                # The default five-second handshake deadline also covers OAuth.
                # Give interactive sign-in a bounded five-minute window instead.
                **({"init_timeout": 300} if auth is not None else {}),
            )
        # Hidden until Pydantic AI's auto-injected ToolSearch reveals them, so a
        # server's schemas cost one `search_tools` call instead of every request.
        if not config.direct:
            toolset = toolset.defer_loading()
        return toolset.prefixed(f"mcp_{name}")
    except (ValueError, TypeError):
        raise ValueError(
            f"Cannot configure MCP server '{name}'; check its transport options."
        ) from None


def deferred_schemas_rejected(error: BaseException) -> bool:
    """Whether the provider rejected this request's hidden tool schemas.

    Deferred loading is a request-shape choice, not history: a provider that
    will not pair withheld schemas with its tool search rejects every request
    the same way, so the session is stuck until the tools are sent in full.
    Narrow on purpose -- a 400 naming the search surface is the provider saying
    this pairing is impossible; any other 400 is a request pcode built wrong.
    """
    from pydantic_ai.exceptions import ModelHTTPError

    return (
        isinstance(error, ModelHTTPError)
        and error.status_code == 400
        and ("tool_search" in str(error.body) or "defer_loading" in str(error.body))
    )


def _find_cause(error: BaseException, kind: type[BaseException]) -> BaseException | None:
    seen: set[int] = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, kind):
            return current
        pending.extend([current.__cause__, current.__context__])
        pending.extend(getattr(current, "exceptions", ()))
    return None


def error_message(error: BaseException) -> str:
    """Show redacted MCP causes, not model/provider troubleshooting advice."""
    from pcode.diagnostics import error_details

    detail = error_details(error)
    messages = []
    while detail:
        message = " ".join(str(detail.get("message", "")).split())
        summary = detail["type"] + (f": {message}" if message else "")
        if summary not in messages:
            messages.append(summary)
        detail = detail.get("cause", detail.get("context", {}))
    return (
        "MCP connection or sign-in failed. "
        + " Caused by: ".join(messages)[:2000]
        + " Check the MCP server configuration, authentication, and connectivity."
    )


@functools.cache
def _isolated() -> type:
    """A per-server layer that turns a failed connection into an empty toolset.

    pydantic-ai enters every run toolset in one exit stack, so one server whose
    handshake fails would otherwise fail the whole turn and take every other
    tool, built-in or MCP, down with it.
    """
    from dataclasses import dataclass, field

    from pydantic_ai.toolsets import WrapperToolset

    @dataclass
    class IsolatedServer(WrapperToolset):
        server: str = ""
        state: Any = None
        # Per instance: exit only what this instance entered.
        _entered: bool = field(default=False, init=False, compare=False, repr=False)

        async def __aenter__(self):
            try:
                await self.wrapped.__aenter__()
            except Exception as error:  # CancelledError is not an Exception.
                self.state.connect_failed(self.server, error)
            else:
                self._entered = True
                self.state.unavailable.pop(self.server, None)
            return self

        async def __aexit__(self, *args: Any) -> bool | None:
            if not self._entered:
                return None
            self._entered = False
            return await self.wrapped.__aexit__(*args)

        # Keyed on the shared state, not `_entered`: pydantic-ai may hand these
        # calls a per-step copy of this wrapper rather than the entered instance.
        async def get_tools(self, ctx):
            if self.server in self.state.unavailable:
                return {}
            return await self.wrapped.get_tools(ctx)

        async def get_instructions(self, ctx):
            if self.server in self.state.unavailable:
                return None
            return await super().get_instructions(ctx)

    return IsolatedServer


class MCPState:
    """Never persisted. Disabled servers have no toolsets, connections, or prompt cost."""

    def __init__(self) -> None:
        self.enabled: dict[str, Any] = {}
        # Captured at enable, like the toolset, so a later config edit changes
        # neither until the server is disabled and enabled again.
        self.descriptions: dict[str, str] = {}
        # Enabled servers whose latest connection attempt failed, with a message
        # safe to show (fixed text and an exception type, nothing the server
        # sent). Cleared by the next successful connection, retried every turn.
        self.unavailable: dict[str, str] = {}
        # Set by the runtime to surface a failure and save its frames.
        self.on_connect_failure: Callable[[str, BaseException], None] = lambda name, error: None

    def connect_failed(self, name: str, error: BaseException) -> None:
        self.unavailable[name] = (
            f"MCP server '{name}' failed to connect ({type(error).__name__}); "
            "continuing without its tools this turn."
        )
        self.on_connect_failure(name, error)

    async def enable(self, name: str, *, interactive: bool = True) -> None:
        if name in self.enabled:
            return
        servers = configured_servers()
        if name not in servers:
            raise ValueError(f"Unknown MCP server '{name}'. Use /mcp list.")
        # Read before building, so a bad entry fails here, not after enabling.
        description = _config(name, servers[name]).description
        toolset = build_toolset(name, servers[name], interactive=interactive)
        # Only OAuth needs an enable-time connection. Entering the MCP toolset
        # initializes the server and completes native auth without a model call.
        # Publish it only after successful login AND connection cleanup, retaining
        # the same OAuth object (and its loaded tokens) for subsequent turns.
        if getattr(mcp_transport(toolset), "auth", None) is not None:
            try:
                async with toolset:
                    pass
            except Exception as error:
                # FastMCP wraps connection failures; callers need the sign-in
                # verdict itself to tell "run /mcp enable" from a real failure.
                from pcode.mcp_oauth import SignInRequired

                if (sign_in := _find_cause(error, SignInRequired)) is not None:
                    raise sign_in from error
                raise
        self.enabled[name] = toolset
        if description:
            self.descriptions[name] = description

    async def forget(self, name: str) -> None:
        """Drop stored OAuth credentials for a server, and its toolset if enabled."""
        self.enabled.pop(name, None)
        self.descriptions.pop(name, None)
        self.unavailable.pop(name, None)
        servers = configured_servers()
        if name not in servers:
            raise ValueError(f"Unknown MCP server '{name}'. Use /mcp list.")
        config = _config(name, servers[name])
        if config.auth != "oauth":
            raise ValueError(f"MCP server '{name}' does not use OAuth.")
        from fastmcp.client.auth.oauth import TokenStorageAdapter

        from pcode.mcp_oauth import CredentialStore

        await TokenStorageAdapter(CredentialStore(), config.url.rstrip("/")).clear()

    def disable(self, name: str) -> None:
        # No config read: disabling must work even if the config was removed or broken.
        if name not in self.enabled:
            raise ValueError(f"MCP server '{name}' is not enabled.")
        del self.enabled[name]
        self.descriptions.pop(name, None)
        self.unavailable.pop(name, None)

    def toolsets(self) -> list:
        """The enabled servers, isolated so one that cannot connect costs only its tools."""
        isolated = _isolated()
        return [
            isolated(toolset, server=name, state=self) for name, toolset in self.enabled.items()
        ]

    def servers(self) -> dict[str, str | None]:
        """Enabled server names, sorted, with the descriptions configured for them."""
        return {name: self.descriptions.get(name) for name in sorted(self.enabled)}

    def undefer(self) -> list[str]:
        """Send every enabled server's schemas up front, for the rest of the session.

        The recovery for a provider that rejects withheld schemas: the tools stay
        available (at their full prompt cost) instead of the session failing every
        request. Returns the servers that were still deferring.
        """
        undeferred = []
        for name, toolset in list(self.enabled.items()):
            if (direct := _without_deferred_loading(toolset)) is not None:
                self.enabled[name] = direct
                undeferred.append(name)
        return undeferred
