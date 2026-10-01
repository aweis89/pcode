# MCP servers

MCP is **off by default**, with no automatic discovery. Configuring a server does
not start it or add its tools to model requests. Use:

```text
/mcp list
/mcp enable fetch
/mcp disable fetch
/mcp logout my-service
```

`/mcp` also lists servers and shows the configuration path. Tab completion offers
configured servers for `enable`, active ones for `disable`, and OAuth servers for
`logout`. These are local commands and make no model request. Enable servers one
at a time, or mark the ones you always want with `"enabled": true` (see
[default-on servers](#default-on-servers)).

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

Replace the placeholder URL with your server's endpoint. The `fetch` example needs
`uvx` on `PATH` and downloads `mcp-server-fetch` on first use. Only configure and
enable servers you trust.

Set `PCODE_MCP_CONFIG=/absolute/path/to/mcp.json` to use a different file.
MCP files in a repository are **not** loaded. Each server under `mcpServers` uses
exactly one transport:

- **Local stdio:** `command`, optional `args` (string array), `env` (string map),
  and `cwd`. The command runs directly, not through a shell. Relative paths are
  resolved from the directory pcode was launched in, so prefer absolute paths.
- **Remote HTTP/SSE:** `url` and optional `headers` (string map); the transport is
  inferred from the URL. Add `"auth": "oauth"` for browser sign-in, plus
  `client_id` and `client_secret` when the service needs a
  [pre-registered client](#pre-registered-clients).

Either transport also accepts `"enabled": true` (on in every conversation),
`"direct": true` (see [tool search](#tool-search-direct)), and a `"description"`
(see [what the model is told](#what-the-model-is-told)).

Server names start with a letter and contain letters, digits, `_`, or `-` (up to
32 characters). Unsupported fields are rejected on enable, not ignored. String
values support `${VARIABLE}` and `${VARIABLE:-default}`, expanded only for the
server being enabled, so a missing credential for an unused server does not get
in the way. Keep secrets in the environment rather than the JSON file.

## OAuth sign-in

Remote servers can use browser-based OAuth sign-in with no separate auth tool:

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

Run `/mcp enable my-service`. pcode connects right away and opens your default
browser if sign-in is needed; finish it there. **No prompt or model request is
needed.** The server is enabled only once sign-in and connection succeed; failure,
Ctrl+C, or `/quit` leaves it off.

While you sign in, the input stays editable and `/mcp list` still works (it never
connects or opens a browser). Queued prompts wait for the server; if sign-in fails
or is cancelled they are cleared rather than run without its tools. Sign-in needs
a browser and a reachable local callback; there is no headless or device-code
login.

- Servers without dynamic client registration need a
  [pre-registered client](#pre-registered-clients). Custom scopes and fixed
  callback ports are not configurable yet.
- **Sign-ins are saved** in `~/.config/pcode/mcp-credentials.json` (owner-only),
  keyed by server URL. Disabling and re-enabling, `/new`, resume, and restart
  reuse them, refreshing silently; the browser opens again only when the service
  rejects the refresh or after `/mcp logout NAME`. Nothing is written to
  `mcp.json` or session files, and there is no keychain integration.
- Two pcode processes refreshing the same server at once can invalidate each
  other's sign-in if the service rotates refresh tokens; the loser is asked to
  sign in again on its next enable.
- Do not combine OAuth with an `Authorization` header; other headers are fine.
  For a static bearer token, use `headers` with an environment variable instead
  of `auth`.
- `/mcp logout NAME` deletes that server's saved sign-in and disables it. Neither
  it nor `/mcp disable` revokes access on the service's side; do that with the
  service if needed.

### Pre-registered clients

Some services, Google's Workspace MCP servers among them, do not support dynamic
client registration. Create an OAuth client with the provider and give pcode its
ID and secret, keeping the secret in the environment:

```json
{
  "mcpServers": {
    "gdrive": {
      "url": "https://drivemcp.googleapis.com/mcp/v1",
      "auth": "oauth",
      "client_id": "${GOOGLE_MCP_CLIENT_ID}",
      "client_secret": "${GOOGLE_MCP_CLIENT_SECRET}"
    }
  }
}
```

`client_id` requires `"auth": "oauth"`, and `client_secret` requires `client_id`.
Sign-in redirects to `http://127.0.0.1:PORT/callback` on a different free port
each time, so the client must accept any loopback port. For Google, create a
**Desktop app** client (Google Auth Platform > Clients), which does; a Web
application client accepts only the exact redirect URIs listed on it. Google's
[Drive MCP setup](https://developers.google.com/workspace/drive/api/guides/configure-mcp-server)
also needs the Drive API and Drive MCP API enabled on the project and the Drive
scopes added to its consent screen. Without a client ID, sign-in fails with
`Registration failed: 400`.

## Default-on servers

Set `"enabled": true` to enable a server whenever a conversation starts: at
launch, after `/new`, and on resume. `/mcp enable NAME --save` does it for you:
it enables the server now and, once that succeeds, writes `"enabled": true` into
`mcp.json`. `/mcp disable NAME --save` turns it off and removes the setting.

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

This works like `/mcp enable` except it **never opens a browser**. An OAuth
server uses its saved sign-in; with none, or one the service rejects, it stays
off and pcode prints `run /mcp enable NAME`. Once you have signed in that way,
later starts are silent. Queued prompts wait for default servers, Ctrl+C cancels,
and `/mcp disable NAME` still turns one off for the current conversation.
`/mcp list` marks these servers `(default on)`.

## Tool search (`direct`)

By default the model does not see every enabled server's tools up front. It gets
a tool search instead and looks up the tools it needs, so a server with fifty
tools costs one search rather than fifty tool definitions in every request.
Where the provider has no built-in tool search, pcode supplies its own, shown as
**Find tools**.

Set `"direct": true` to send a server's tools up front instead:

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

That suits small servers whose one or two tools you expect every turn, since it
saves the search. The setting is per server, so direct and searched servers can
be enabled together.

If a provider rejects a request that uses its tool search, pcode says so, sends
every tool up front for the rest of the session, and retries the turn. The tools
keep working at their full prompt cost.

## What the model is told

So the model knows to search for hidden tools, pcode tells it which servers are
enabled. The list is sent at the start of a conversation with servers enabled and
again whenever it changes (after `/mcp enable` or `/mcp disable`, or when
compaction drops it). Turning the last server off tells the model none are
enabled. Delegated workers and side questions (`/btw`) see the same list.

```text
<mcp-servers>
Enabled MCP servers (replaces any earlier list):
- gdrive
- cs: CodeSignal assessments and candidates
</mcp-servers>
```

Changing the list does not invalidate the prompt cache, but enabling the first
server whose tools are searched does, because it adds the search tool. On
`claude:` models each search that finds new tools does too: the Claude Code CLI
has no way to mark a tool as deferred, so a found tool's definition is sent for
the first time, ahead of the whole cached conversation. Searching early in a
conversation keeps that rewrite small; a server you use constantly can set
[`"direct": true`](#tool-search-direct) instead, at the cost of sending all its
schemas on every request.

The name is often enough. Add a `description` when it is not:

```json
{
  "mcpServers": {
    "cs": {
      "command": "cs-mcp",
      "description": "CodeSignal assessments and candidates"
    }
  }
}
```

Keep it to a short phrase. It is read when the server is enabled, so after
editing it, disable and enable the server again.

## Activation and token usage

- `/mcp enable NAME` makes a server's tools available in the **current
  conversation**. Switching models keeps the selection, and enabling twice does
  nothing.
- You can enable or disable while the model is working. The running turn picks
  up the change at its next model request, the way a steering message does.
  Messages you send after `/mcp enable` wait until it finishes, so "use the new
  server" reaches the model together with that server's tools.
- `/mcp enable-all` enables every configured server that is not already on, one
  at a time, opening a browser for any OAuth sign-in. A server that fails stays
  off and the rest still enable.
- OAuth servers connect during `/mcp enable` to sign in; other servers first
  connect on the next turn. A server that accepts the connection without
  credentials and only rejects tool calls enables silently, and its first
  authentication error appears mid-turn.
- Enabled servers connect at the start of each turn (or, if enabled during one,
  at its next request) and close after it, even on failure or cancellation. Local server processes do not keep running between
  turns.
- A server that fails to connect costs only its own tools. The turn goes ahead,
  pcode prints a warning naming it (details go to the session's `errors.log`),
  and the model is told it failed to connect this turn. It stays enabled and is
  retried next turn; `/mcp disable NAME` stops that.
- `/mcp disable NAME` removes its tools from later requests, including the rest
  of a running turn. To reload a server after changing its configuration or
  environment, disable and enable it again.
- `/new`, resumed conversations, and restarts start with **all servers off**
  except those marked `"enabled": true`. Which servers are on is saved only when
  you add `--save` to `/mcp enable` or `/mcp disable`.
- Off servers add nothing to requests. Enabled servers cost context for their
  results, and for their tool definitions once they are direct or found by search.
  Disabling does not remove earlier results from history.
- Enabling a server lets the agent use its tools with that server's permissions,
  including write actions. There is no per-call approval or sandbox. The server's
  own instructions are not added to the prompt.
