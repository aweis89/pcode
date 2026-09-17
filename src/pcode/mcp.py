"""Explicit, conversation-scoped MCP activation; configuration alone does nothing."""

import json
import os
import re
import warnings
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from pcode.preferences import preferences_path

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


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

    @model_validator(mode="after")
    def transport(self):
        if bool(self.command) == bool(self.url):
            raise ValueError("Specify exactly one of command or url.")
        if self.command:
            if not self.command.strip() or self.headers is not None or self.auth is not None:
                raise ValueError("Invalid stdio options.")
        else:
            if any(item is not None for item in (self.command, self.args, self.env, self.cwd)):
                raise ValueError("Invalid HTTP options.")
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Expected an HTTP(S) URL.")
            if self.auth and any(key.lower() == "authorization" for key in self.headers or {}):
                raise ValueError("OAuth cannot be combined with an Authorization header.")
        return self


def build_toolset(name: str, raw: Any):
    """Construct only the selected server. Connections are owned by each agent run."""
    try:
        config = ServerConfig.model_validate(_expand(raw))
    except ValidationError:
        # Pydantic errors include input values: never print credentials from config.
        raise ValueError(
            f"Invalid MCP server '{name}'. Use command/args/env/cwd for stdio or url/headers "
            'for HTTP (optional auth: "oauth"); other fields are not supported.'
        ) from None
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
            # FastMCP owns PKCE, browser/callback handling, refresh, and an in-memory
            # token store. Surface its storage lifetime in our UI/docs, not a raw
            # warning that would interrupt the prompt. Do not suppress other warnings.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="Using in-memory token storage -- tokens will be lost.*",
                    category=UserWarning,
                )
                toolset = MCPToolset(config.url, id=name, headers=config.headers, auth=config.auth)
        return toolset.prefixed(f"mcp_{name}")
    except (ValueError, TypeError):
        raise ValueError(
            f"Cannot configure MCP server '{name}'; check its transport options."
        ) from None


class MCPState:
    """Never persisted. Disabled servers have no toolsets, connections, or prompt cost."""

    def __init__(self) -> None:
        self.enabled: dict[str, Any] = {}

    def enable(self, name: str) -> None:
        if name in self.enabled:
            return
        servers = configured_servers()
        if name not in servers:
            raise ValueError(f"Unknown MCP server '{name}'. Use /mcp list.")
        self.enabled[name] = build_toolset(name, servers[name])

    def disable(self, name: str) -> None:
        # No config read: disabling must work even if the config was removed or broken.
        if name not in self.enabled:
            raise ValueError(f"MCP server '{name}' is not enabled.")
        del self.enabled[name]

    def toolsets(self) -> list:
        return list(self.enabled.values())
