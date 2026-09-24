# MCP servers

MCP is **off by default**, with no automatic discovery. Configuring a server does
not start it or add its tool definitions to model requests. Use:

```text
/mcp list
/mcp enable fetch
/mcp disable fetch
/mcp logout my-service
```

`/mcp` also lists servers and the configuration path. Tab completion includes
configured server names for `enable`, active names for `disable`, and OAuth
servers for `logout`. These are local commands; they do not make a model request.
Enable servers individually, or mark the ones you always want with
`"enabled": true` (see [default-on servers](#default-on-servers)).

Create `~/.config/pcode/mcp.json` (or `$XDG_CONFIG_HOME/pcode/mcp.json`):

```json
{
  "mcpServers": {
    "fetch": {
      "command": "uvx",
      "args": ["mcp-server-fetch"]
    },
    "internal": {
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${INTERNAL_MCP_TOKEN}"
      }
    }
  }
}
```

The remote URL is a placeholder; replace it with your server's endpoint. The
`fetch` example requires `uvx` on PATH and downloads/runs `mcp-server-fetch` on
first use. Only configure and enable servers you trust.

Set `PCODE_MCP_CONFIG=/absolute/path/to/mcp.json` to use a different file.
Repository MCP files are **not** loaded automatically. The JSON uses an
`mcpServers` object, with each server configured for exactly one transport:

- **Local stdio:** `command`, optional `args` (string array), `env` (string map),
  and `cwd`. Commands are executed directly, not through a shell. Relative paths
  are resolved from pcode's launch directory; prefer absolute paths.
- **Remote HTTP/SSE:** `url` and optional `headers` (string map). Transport is
  inferred from the URL by the MCP client. Add `"auth": "oauth"` for browser sign-in,
  plus `client_id` and `client_secret` when the service needs a
  [pre-registered client](#pre-registered-clients).

Either transport also accepts `"enabled": true` (on for every conversation) and
`"direct": true` (see tool search below).

Server names start with a letter and contain letters, digits, `_`, or `-` (up to
32 characters). Unsupported server fields are rejected on enable rather than
silently ignored. String values support `${VARIABLE}` and `${VARIABLE:-default}`.
Only the selected server's variables are expanded, at enable time, so missing
credentials for an unused server do not block ordinary work. Keep secrets in the
environment rather than the JSON file.

## OAuth sign-in

Remote servers can use the browser-based OAuth support built into Pydantic AI and
FastMCP. No separate auth tool or custom OAuth flow is needed:

```json
{
  "mcpServers": {
    "my-service": {
      "url": "https://mcp.example.com/mcp",
      "auth": "oauth"
    }
  }
}
```

Replace the placeholder URL with your server, then run `/mcp enable my-service`.
The command immediately connects, discovers the server's OAuth settings, and opens
your default browser if sign-in is needed. Finish sign-in in the browser through
the temporary localhost callback server. **No prompt or model request is needed.**
The server becomes enabled only after authentication and MCP initialization succeed;
failure or Ctrl+C leaves it off. `/quit` also cancels a pending login.

The input remains editable and `/mcp list` stays available during sign-in. Queued
prompts wait for successful activation; failure or cancellation clears them rather
than running without the requested tools. `/mcp list` itself never connects or
opens a browser. This requires a browser and a reachable local callback; there is
no headless/device-code login command.

- Pydantic AI's `MCPToolset(auth="oauth")` delegates PKCE, dynamic client registration,
  callback/state validation, token refresh, and authenticated requests to FastMCP
  and the MCP SDK. Custom scopes and fixed callback ports are not exposed in
  pcode's config; a server without dynamic registration needs a
  [pre-registered client](#pre-registered-clients).
- **Sign-ins are saved.** Tokens and the client registration are written to
  `~/.config/pcode/mcp-credentials.json` (owner-readable only, next to the
  Anthropic sign-in), keyed by server URL. Disable/re-enable, `/new`, resume, and
  restart reuse them, refreshing the access token silently; the browser opens
  again only when the refresh token is rejected or after `/mcp logout NAME`.
  Nothing is written to `mcp.json` or session files. There is no keychain
  integration: pcode is often run over SSH, where a keychain is locked, and the
  file mirrors how pcode already stores its own login.
- Two pcode processes that refresh the same server at once can invalidate each
  other's refresh token if the service rotates them; the loser is asked to sign
  in again on its next enable.
- Do not combine OAuth with an `Authorization` header. Non-auth headers may be used
  alongside OAuth. For a static bearer token, continue using `headers` with an
  environment variable reference instead of `auth`.
- `/mcp logout NAME` deletes the saved credentials for that server and disables
  it. Neither that nor `/mcp disable` revokes the server-side grant; revoke access
  through the service if needed.

### Pre-registered clients

Some authorization servers, Google's among them, don't let a client register
itself. Create an OAuth client in the service's console and give pcode its ID
(and secret, if it has one). Reference them from the environment so they stay
out of the file:

```json
{
  "mcpServers": {
    "gdrive": {
      "url": "https://drivemcp.googleapis.com/mcp/v1",
      "auth": "oauth",
      "client_id": "${GDRIVE_OAUTH_CLIENT_ID}",
      "client_secret": "${GDRIVE_OAUTH_CLIENT_SECRET}"
    }
  }
}
```

pcode's callback listens on `http://127.0.0.1:<port>/callback` with a port chosen
at sign-in, so the client must accept any loopback port. For Google, create a
**Desktop app** client (Google Auth Platform > Clients), which does; a Web
application client only accepts the exact redirect URIs you list. Google's
[Drive MCP setup](https://developers.google.com/workspace/drive/api/guides/configure-mcp-server)
also needs the Drive API and Drive MCP API enabled on that project and the Drive
scopes added to its consent screen. Without a client ID, sign-in fails with
`Registration failed: 400`.

## Default-on servers

Set `"enabled": true` on a server to have it enabled when a conversation starts,
i.e. at launch, after `/new`, and on resume:

```json
{
  "mcpServers": {
    "my-service": {
      "url": "https://mcp.example.com/mcp",
      "auth": "oauth",
      "enabled": true
    }
  }
}
```

This is the ordinary `/mcp enable` with one difference: it **never opens a
browser**. An OAuth server uses its saved sign-in (refreshing if needed); with no
saved sign-in, or one the service rejects, it stays off and pcode prints
`run /mcp enable NAME`. That one interactive sign-in is then saved, so later
starts are silent. Queued prompts wait for the default servers, Ctrl+C cancels,
and `/mcp disable NAME` still turns one off for the current conversation. `/mcp
list` marks these servers `(default on)`.

## Tool search (`direct`)

MCP tools are **deferred** by default: the model sees a `search_tools` function
instead of every enabled server's schemas, and calls it to reveal the tools it
needs. A server with fifty tools then costs one search call rather than fifty
schemas in every request of the conversation.

Set `"direct": true` on a server to send its tool definitions up front instead:

```json
{
  "mcpServers": {
    "fetch": {
      "command": "uvx",
      "args": ["mcp-server-fetch"],
      "direct": true
    }
  }
}
```

That is worth it for small servers whose one or two tools you expect every turn,
since it saves the discovery round trip. Deferral is per server, so direct and
searched servers can be enabled together.

Discovery is handled by Pydantic AI's auto-injected `ToolSearch` capability:
natively by the provider where supported (recent Anthropic and OpenAI models),
otherwise by a local `search_tools` tool that pcode shows as **Find tools**.
Either way the revealed tools keep their `mcp_NAME_TOOL` names, and the search
exchange is appended to history, so the prompt cache prefix stays intact.

If a provider rejects a request that hides schemas behind its own tool search,
pcode stops deferring for the rest of the session, says so, and sends the turn
again with every tool declared up front, as `"direct": true` would. The tools
stay usable at their full prompt cost rather than the session failing every
request.

## Activation and token usage

- `/mcp enable NAME` makes that server's tools available on subsequent turns in
  the **current conversation**, including all tool/model steps within a turn.
  Switching models keeps the selection. Repeating `enable` is a no-op.
- OAuth servers connect during `/mcp enable`, then disconnect while retaining
  their tokens. Non-OAuth servers still connect only on the next turn.
  Sign-in is challenge-driven: a server that answers `initialize` and
  `tools/list` without credentials and only rejects the tool calls themselves
  never triggers the browser flow at enable time, so `/mcp enable` succeeds
  silently and the first authentication error arrives mid-turn.
  All enabled servers reconnect for each turn and close afterward, including on
  failure or cancellation; local subprocesses do not stay running between turns.
- `/mcp disable NAME` removes those tools from subsequent model requests. MCP
  selection cannot change during an active turn. To reload a server after editing
  its configuration or environment, disable and enable it again.
- New conversations (`/new`), resumed conversations, and application restarts
  start with **all servers off** except those marked `"enabled": true` in
  `mcp.json`. Activation is never saved in session files or user defaults.
  `/mcp list` shows the current state.
- Off servers contribute **no MCP tool schemas or server instructions**. Enabled
  tools are namespaced as `mcp_NAME_TOOL`; their results consume context normally,
  and their schemas do too once they are direct or discovered. Disabling does not
  erase earlier tool results from history.
- Enabling authorizes the agent to use the server's tools with that server's
  permissions, including write actions. There is no additional per-call approval
  or sandbox. Server instructions are not automatically added to the prompt.
