# Legacy Anthropic sign-in and Meridian

pcode's own Anthropic browser sign-in (`/login` for `anthropic:` models) and the
`meridian:` provider are both gated by `LEGACY_ANTHROPIC_AUTH` in
`src/pcode/models.py`, which is `False`. While it is off:

- `/login` accepts only `claude` and `openai-codex`; a bare `/login` signs in to
  Claude Code.
- `anthropic:` models always use `ANTHROPIC_API_KEY`. A stored
  `credentials.json` sign-in, a saved `anthropic_auth` preference, and
  `PCODE_ANTHROPIC_AUTH` are ignored (`anthropic_oauth.py`).
- `/model` never offers `meridian`, and a saved `meridian:` model fails to start
  with a pointer to the matching `claude:` model (`agent.py`).
- `pcode --upgrade-meridian` still installs or upgrades the npm package, and the
  `meridian_managed` and `anthropic_auth` preferences can still be set, but none
  of them makes either feature reachable.

Setting the flag to `True` restores both features unchanged. The rest of this
note is the user documentation they had in `docs/providers.md`, kept here so it
can be republished if they come back. See
[anthropic-providers.md](anthropic-providers.md) for the account risk and the
design comparison, and [meridian-validation.md](meridian-validation.md) for
Meridian testing.

## Sign in with your Anthropic account

Enter `/login` in an idle session to use your Anthropic subscription with
`anthropic:` models. pcode prints the authorization URL and opens `claude.ai` in
your browser; with a current browser session it is a single approval click. The
active conversation keeps its history and switches to the new credential.

```sh
pcode -m anthropic:<model-id>   # then: /login
```

- The browser redirects to `http://localhost:54545/callback`. Set
  `PCODE_OAUTH_CALLBACK_PORT` if that port is taken. The callback must be
  reachable from the browser; over SSH, forward it with
  `ssh -L 54545:localhost:54545`. A callback from anything but this sign-in gets
  an error page while the sign-in keeps waiting. Sign-in times out after five
  minutes.
- Tokens are stored in `~/.config/pcode/credentials.json` with owner-only (0600)
  permissions (`XDG_CONFIG_HOME`, `PCODE_CONFIG_DIR`, and `PCODE_CREDENTIALS_FILE`
  are honored). They are refreshed automatically, without blocking the terminal.
  No API key is created, and nothing is written to another tool's credential
  store.
- `/logout` removes the stored credential. Neither command works while a run or
  queued prompts are active.
- Later launches use the stored login ahead of `ANTHROPIC_API_KEY`. Set
  `PCODE_ANTHROPIC_AUTH=api-key` to force the environment key, or
  `PCODE_ANTHROPIC_AUTH=oauth` to require this login.
- Requests go to `https://api.anthropic.com` regardless of `ANTHROPIC_BASE_URL`.

**Account risk.** This sign-in authenticates as the public Claude Code client.
It is compatibility support, not an official third-party integration, and
entitlements, quotas, and server behavior can change at any time. The supported
path is `ANTHROPIC_API_KEY`. See [Anthropic provider options](anthropic-providers.md)
for the account risk and the alternatives.

If a model is rejected with `400 claude_code_version_too_old`, the Claude Code
version pcode reports is too old for it. pcode reports your installed
`claude --version` when that is newer than its built-in value, so keeping Claude
Code updated usually fixes it. Without Claude Code installed, set
`PCODE_CLAUDE_VERSION` (for example `2.1.280`). An older or never-released
version gets requests rejected.

There is no API-key entry UI. For API-key access, set `ANTHROPIC_API_KEY` in your
environment.

## Local Meridian provider

[Meridian](https://github.com/rynfar/meridian) runs Claude Code behind a local
Anthropic-compatible API, so a `meridian:` model uses your Claude subscription.
[Anthropic provider options](anthropic-providers.md) compares it with `/login`.

```sh
pcode --upgrade-meridian            # install or upgrade Meridian with npm
pcode -m meridian:claude-sonnet-5   # then /login meridian if Claude is not signed in
```

Meridian is an npm package, so the Homebrew formula does not install it, and
`pcode --upgrade-meridian` needs `npm` on `PATH`. Once `meridian` is on `PATH`,
pcode starts its own private instance with no further setup, unless an older
shared proxy already answers on port 3456 (see below).

### Which Meridian pcode uses

The `meridian_managed` preference decides which Meridian pcode connects to:

| Value | Behavior |
| --- | --- |
| `auto` (default) | Use a proxy already answering at `http://127.0.0.1:3456`; otherwise start a private one when `meridian` is on `PATH` |
| `on` | Always start a private instance |
| `off` | Always use the external proxy, running or not |

Setting `PCODE_MERIDIAN_BASE_URL` always selects that external proxy.
`PCODE_MERIDIAN_MANAGED=1` or `0` overrides the preference for one process (as
`on` or `off`); config commands show the saved value, not this override. A change
applies the next time pcode connects to Meridian and does not stop an instance
already running.

**Private instance.** Each pcode process starts one on a free loopback port while
the terminal opens, and it needs Meridian 1.71.1 or newer. It is configured
separately from any Meridian you run yourself (your `MERIDIAN_*` and
`CLAUDE_PROXY_*` variables are ignored), with Thinking Passthrough on and
telemetry and update checks off. If it is not ready within 30 seconds, pcode
reports the failure.

It uses your saved active Meridian profile, else the first profile, else Claude
Code's own login. pcode never reads or copies the credential.

Its session store is `$XDG_STATE_HOME/pcode/meridian/sessions` (default
`~/.local/state/pcode/meridian/sessions`), shared by all pcode processes, so a
resumed conversation continues where it left off. If the instance exits, pcode
restarts it within about a second; requests in flight are not replayed, and after
three restarts in five minutes pcode gives up and says so. A normal pcode exit
stops the instance, but `kill -9` cannot. After Meridian itself crashes, the
replacement can answer `503 overloaded_error` for about a minute.

**External instance.** The default is `http://127.0.0.1:3456`; override it with
`PCODE_MERIDIAN_BASE_URL` (the server root, without `/v1/messages`). If your proxy
needs an API key, set `PCODE_MERIDIAN_API_KEY`. pcode never changes an external
proxy's settings.

If you run Meridian as a service (launchd, systemd, `brew services`), set
`MERIDIAN_WORKDIR` to an existing empty directory. Otherwise Meridian usually runs
in `/`, and Claude Code scans every file under it on each request: requests slow
down, and under load Meridian answers `503 overloaded_error` ("session
bookkeeping is saturated"). The private instance does this for you.

Meridian owns authentication, so `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and
`ANTHROPIC_BASE_URL` are not passed to it. Global HTTP proxy settings and
`PCODE_LLM_PROXY` do not apply. If Meridian is unavailable, requests fail rather
than going to Anthropic directly.

### Signing in

`/login meridian` runs `claude auth login` for the login your Meridian reads: the
active profile of an external proxy that has profiles, the profile a private
instance started with, or otherwise Claude Code's own login. Sign-in completes in
Anthropic's own flow, pcode stores nothing, and the next request uses it. It needs
`claude` on `PATH` (or `MERIDIAN_CLAUDE_PATH`).

Over SSH, or when no browser opens, `/login meridian` prints the command to run in
a terminal on that machine instead. For a profile that authenticates with a
`claude setup-token` token, it prints `meridian profile add NAME --oauth-token`.
`/login` without an argument is still pcode's own Anthropic sign-in.

### Upgrading Meridian

`pcode --upgrade-meridian` upgrades every Meridian pcode can use, each with the
npm that installed it: the `meridian` on `PATH` and the one the running proxy was
started from. With neither installed, it installs `@rynfar/meridian`. A running
proxy keeps its old version until it restarts; the command says so and, when a
macOS launchd agent runs Meridian, prints the `launchctl kickstart -k` line to
restart it. Private instances update when pcode restarts.

### Models, errors and thinking

`/model` includes Meridian when `meridian` is on `PATH`, when
`PCODE_MERIDIAN_BASE_URL` is set, or when the current model is Meridian. It
suggests Claude model IDs; type `meridian:<model-id>` for others your proxy
supports. Listing does not start Meridian or check model access. pcode, not
Meridian, runs the tools.

When a request fails, the error names the cause when pcode can tell: the proxy
not answering, a private instance restarting, a Claude login to refresh with
`/login meridian`, or a key the proxy rejected.

To see thinking, `/show-thinking` must not be `off` and Meridian must forward
thinking. A private instance does this already. For an external proxy,
`/show-thinking` reports the proxy's setting, and pcode warns once per session
when thinking display is on but the proxy is not forwarding it. Turn on the
passthrough adapter's Thinking Passthrough option on the proxy's `/settings` page
(default <http://127.0.0.1:3456/settings>); it is off by default and affects every
client of that proxy, so pcode leaves it to you. Thinking the model never sent
still cannot be shown.

