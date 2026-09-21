"""Explicit, conversation-scoped MCP activation; configuration alone does nothing."""

import json
import os
import re
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
    # Enable at startup, /new, and resume instead of waiting for /mcp enable.
    enabled: bool = False
    # Off by default: a server's schemas otherwise sit in every request of the
    # conversation, while tool search costs one call for the tools actually used.
    direct: bool = False

    @model_validator(mode="after")
    def transport(self):
        if bool(self.command) == bool(self.url):
            raise ValueError("Specify exactly one of command or url.")
        if self.command:
            if not self.command.strip() or self.headers is not None or self.auth is not None:
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
            'for HTTP (optional auth: "oauth", enabled: true, direct: true); other fields '
            "are not supported."
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

            auth = LoopbackOAuth(interactive=interactive) if config.auth == "oauth" else None
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


class MCPState:
    """Never persisted. Disabled servers have no toolsets, connections, or prompt cost."""

    def __init__(self) -> None:
        self.enabled: dict[str, Any] = {}

    async def enable(self, name: str, *, interactive: bool = True) -> None:
        if name in self.enabled:
            return
        servers = configured_servers()
        if name not in servers:
            raise ValueError(f"Unknown MCP server '{name}'. Use /mcp list.")
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

    async def forget(self, name: str) -> None:
        """Drop stored OAuth credentials for a server, and its toolset if enabled."""
        self.enabled.pop(name, None)
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

    def toolsets(self) -> list:
        return list(self.enabled.values())
