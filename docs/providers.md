# Providers and models

## Authentication

For `openai-codex:` models, run `/login openai-codex` to sign in with your
ChatGPT account in the browser. No Codex CLI is required. Credentials are stored
in `codex-credentials.json` under `$PCODE_CONFIG_DIR`, otherwise
`$XDG_CONFIG_HOME/pcode` (default `~/.config/pcode`);
`PCODE_CODEX_CREDENTIALS_FILE` overrides the full path. The file is owner-only,
and refreshed tokens are saved for later launches.

- pcode's own login wins. Without it, pcode reads the Codex CLI's `auth.json` as
  a read-only fallback (`CODEX_HOME` is honored); tokens refreshed from it stay in
  memory. A malformed pcode login is reported as an error rather than silently
  switching accounts.
- `/logout openai-codex` removes only pcode's login, never the CLI's. New models
  then fall back to the CLI login if there is one; the active model keeps its
  cached token.
- The browser callback uses fixed port 1455, so close other login flows using it
  first. On a headless machine, run the Codex CLI's `codex login --device-auth`
  instead.
- This provider never falls back to `OPENAI_API_KEY`. Which models you can use
  depends on your account. Authentication errors never show raw provider
  responses or credential values.

For ordinary OpenAI API models, use `openai:<model-id>` with `OPENAI_API_KEY` in
your environment. For Anthropic API models, use `anthropic:<model-id>` with
`ANTHROPIC_API_KEY`. Use the exact model ID your account offers; pcode does not
remap aliases. The provider extras below are installed by default, including with
`make install`.

## Supported providers

Any `provider:model-id` string that Pydantic AI accepts works with `--model` and
`/model`. The model picker offers a provider when its variable below is *set*
(the value is never read). "Catalog" says whether the picker suggests model IDs
or only accepts IDs you type.

| Prefix | Enabled by | Catalog |
| --- | --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY` (or a `/login` credential, [turned off](#sign-in-with-your-anthropic-account)) | yes |
| `openai-codex` | pcode login, then CLI credential file (`CODEX_HOME` honored) | yes (all OpenAI IDs) |
| `openai`, `openai-chat`, `openai-responses` | `OPENAI_API_KEY` | yes |
| `claude` | Claude Code config (`~/.claude`, `~/.claude.json`, or `CLAUDE_CONFIG_DIR`) or `claude` on `PATH` | yes (Anthropic IDs) |
| `meridian` | [Turned off](#local-meridian-provider); otherwise `meridian` on `PATH` or `PCODE_MERIDIAN_BASE_URL` | yes (Anthropic IDs) |
| `google` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` | yes |
| `google-cloud` | `GOOGLE_CLOUD_PROJECT` or `GOOGLE_APPLICATION_CREDENTIALS` | yes |
| `bedrock` | `AWS_BEARER_TOKEN_BEDROCK`, `AWS_ACCESS_KEY_ID`, or `AWS_PROFILE` | yes |
| `bedrock-mantle` | `AWS_BEARER_TOKEN_BEDROCK` | yes |
| `groq` | `GROQ_API_KEY` | yes |
| `xai` | `XAI_API_KEY` | yes |
| `deepseek` | `DEEPSEEK_API_KEY` | yes |
| `cerebras` | `CEREBRAS_API_KEY` | yes |
| `crusoe` | `CRUSOE_API_KEY` | yes |
| `moonshotai` | `MOONSHOTAI_API_KEY` | yes |
| `heroku` | `HEROKU_INFERENCE_KEY` | yes |
| `snowflake` | `SNOWFLAKE_TOKEN` and `SNOWFLAKE_ACCOUNT` | yes |
| `zai` | `ZAI_API_KEY` | yes |
| `azure` | `AZURE_OPENAI_API_KEY` and `AZURE_OPENAI_ENDPOINT` | typed IDs |
| `github-copilot` | `GITHUB_COPILOT_API_KEY` (or `_TOKEN`/`COPILOT_GITHUB_TOKEN`) and `GITHUB_COPILOT_BASE_URL` (or `_API_BASE`/`COPILOT_API_URL`) | typed IDs |
| `openrouter` | `OPENROUTER_API_KEY` | typed IDs |
| `vercel` | `VERCEL_AI_GATEWAY_API_KEY` or `VERCEL_OIDC_TOKEN` | typed IDs |
| `fireworks` | `FIREWORKS_API_KEY` | typed IDs |
| `together` | `TOGETHER_API_KEY` | typed IDs |
| `nebius` | `NEBIUS_API_KEY` | typed IDs |
| `ovhcloud` | `OVHCLOUD_API_KEY` | typed IDs |
| `alibaba` | `ALIBABA_API_KEY` or `DASHSCOPE_API_KEY` | typed IDs |
| `sambanova` | `SAMBANOVA_API_KEY` | typed IDs |
| `ollama` | `OLLAMA_BASE_URL` | typed IDs |
| `vllm` | `VLLM_BASE_URL` | typed IDs |

The catalog is a static list of known models, not what your account is entitled
to. Providers not in the table (for example `mistral`, `cohere`, `huggingface`,
`litellm`) still work from `--model` once you install their Pydantic AI extra,
but the picker does not offer them. Apart from Anthropic sign-in, Codex and
Claude Code, every provider is plain API-key access with no login in pcode: set
the variable in your shell before launching.

## Sign in with your Anthropic account

!!! note "Turned off"
    This sign-in and [Meridian](#local-meridian-provider) are off while
    [Claude Code models](#claude-code-provider) (`claude:`) are tried as the way
    to use a Claude subscription. `/login` signs in to Claude Code instead, a
    stored sign-in is ignored, and `anthropic:` models use `ANTHROPIC_API_KEY`
    as before. Setting `LEGACY_ANTHROPIC_AUTH = True` in `src/pcode/models.py`
    restores both as described here.

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

!!! warning "Account risk"
    This sign-in authenticates as the public Claude Code client. It is
    compatibility support, not an official third-party integration, and
    entitlements, quotas, and server behavior can change at any time. The
    supported path is `ANTHROPIC_API_KEY`. See
    [Anthropic provider options](https://github.com/aweis89/pcode/blob/master/dev/anthropic-providers.md)
    for the account risk and the alternatives.

If a model is rejected with `400 claude_code_version_too_old`, the Claude Code
version pcode reports is too old for it. pcode reports your installed
`claude --version` when that is newer than its built-in value, so keeping Claude
Code updated usually fixes it. Without Claude Code installed, set
`PCODE_CLAUDE_VERSION` (for example `2.1.280`). An older or never-released
version gets requests rejected.

There is no API-key entry UI. For API-key access, set `ANTHROPIC_API_KEY` in your
environment.

## Choose a model in the terminal

Use `/model` or Ctrl+L to open the model picker. Type to filter, use ↑/↓ to
select, and press Enter to apply. Filtering matches provider and model names,
including joined word prefixes: `anthopus` finds Anthropic Opus, `codluna` finds
Codex Luna, and `opus anth` works too. Escape, Ctrl+C, or Ctrl+L closes the
picker without changing the model or your draft. For a model not listed, type its
full `provider:model-id` (for example `anthropic:claude-opus-5`).

The picker offers each provider in the [supported providers](#supported-providers)
table whose credentials are configured, plus the current provider even with a
custom model ID. Opening it makes no network requests and never reads credential
contents: it only checks that a credential file exists or a variable is set.
`PCODE_LLM_PROXY` does not affect which models are offered.

Models are grouped by provider and family, newest version first (Opus 5 before
Opus 4.8; 4.10 before 4.9), and filtering keeps that order. The current model is
marked, not pinned to the top. Undated aliases come before dated snapshots of the
same version.

Suggestions are not an entitlement list; the provider checks access when you use
the model. A typed `provider:model-id` is accepted for any supported provider,
configured or not, so you can export a key after launch. If no provider is
configured, run `/login claude` or `/login openai-codex`, or set
`ANTHROPIC_API_KEY`, first.

**Changing models continues the current conversation.** History, session ID,
plan, tool panel, usage totals, transcript, and draft are kept, and the saved
session records the new model so resuming uses it. Use `/new` to start over
instead. Selecting the current model does nothing, and a failed switch leaves the
conversation intact. Switching from the offline preview starts a live
conversation without restarting pcode.

The picker also works while a run or queued messages are active. As with
`/effort`, the new model applies from the next request: the turn in flight
finishes on its model, and the footer shows `current → next` until the switch.
Ctrl+C on the running turn keeps the pending selection.

## Saved model and effort defaults

A model chosen with `/model` becomes the default for future launches once the
selection takes effect. `/effort` and Ctrl+N/Ctrl+P save the effort for the
current model only, and also save that model as the default. Preferences live in
`~/.config/pcode/preferences.json` (or `$XDG_CONFIG_HOME/pcode/preferences.json`),
separate from saved conversations and unaffected by `--no-save`.

- `pcode` with no model argument reuses the saved model; with none saved it opens
  the offline preview. `--theme-preview` always stays offline.
- Saved effort applies per model, in new and resumed conversations. A model you
  never set uses the `effort` default. `/effort default` restores the provider
  default for the current model.
- `-m` / `--model` overrides the saved model for one launch, and `--continue`
  uses the session's model. Neither changes the saved default. Model and provider
  names are passed through as given.
- `pcode config unset KEY` resets one default.

## Reasoning effort

For OpenAI/Codex, Anthropic, Claude Code, and Meridian models, Ctrl+N raises
effort and Ctrl+P lowers it; or use `/effort low|medium|high|xhigh`. `/effort`
alone shows the current setting and `/effort default` removes the override. Each
model remembers its own level. The footer shows the selected effort.

- Shortcuts stop at the lowest and highest levels. From the provider default they
  start at medium, so Ctrl+N selects high and Ctrl+P selects low.
- Changes apply from the next turn and keep your draft.
- Effort does not change the model's thinking configuration. Not every model
  accepts every level; the provider still validates it.
- On Anthropic, Claude Code, and Meridian, only models that support effort accept
  it (Opus 4.5+ and Sonnet 4.6+, among others). For any other model, `/effort`
  refuses a level and the footer shows `n/a`; `/effort default` still clears a
  saved level. `xhigh` becomes Anthropic's `max` where the model has no native
  `xhigh`.
- The offline preview and other providers show `n/a`.

## Claude Code provider

A `claude:` model uses your Claude subscription through Claude Code's own login,
with no proxy to install or run:

```sh
pcode -m claude:claude-sonnet-5   # then /login claude if Claude Code is not signed in
```

The Claude Code CLI comes bundled, so you need neither Node.js nor a separate
`claude` install. Because the bundle is large (about 215 MB installed), it is the
optional `claude` extra. The Homebrew formula and `make install` include it;
elsewhere install `pcode[claude]`, for example
`uv tool install --editable '.[claude]'` from a checkout. Without it, `claude:`
models are left out of `/model`, and naming one tells you what to install.

pcode keeps one Claude Code process per conversation and runs every tool itself.
Unlike Meridian, a tool round does not start a new process.
[Anthropic provider options](https://github.com/aweis89/pcode/blob/master/dev/anthropic-providers.md#direct-sdk-provider)
has the design and measurements.

### Signing in

`/login claude` runs `claude auth login` in your browser and writes Claude Code's
usual login (`CLAUDE_CONFIG_DIR` is honored), so an existing Claude Code sign-in
already works. pcode never reads or stores the credential. A request that fails
because the login is missing or expired tells you to run `/login claude`.

`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, and Claude
Code's cloud routes (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, and the
like) are cleared for `claude:` requests and `/login claude`. The key you use for
`anthropic:` models can never bill or redirect a `claude:` request.

### What carries over

Conversations continue with a warm prompt cache after restarting pcode,
`--continue`, `/tree`, a retry, `/btw`, or a model or effort change. When the
history never went through Claude Code (you switched from another provider) or
was rewritten by compaction, the earlier conversation is replayed once, which
writes it to the cache again.

- Memory: each session runs a Claude Code process while a turn is in progress.
  The first costs about 300 MB and each further one about 110–135 MB. After a turn
  ends, each session keeps its process for ten minutes so the next turn starts
  immediately (`claude_idle_processes` and `claude_idle_minutes`; `0` processes
  stops each one when its turn ends). A process waiting on tool results, such as
  a parent waiting for delegated tasks, is kept up to thirty minutes. When less
  than a tenth of the machine's memory is free, pcode stops idle processes within
  a minute. All of them stop when pcode exits.
- Transcripts, tool output included, are stored in Claude Code's own store
  (`~/.claude/projects/`) and appear in `claude --resume` for the workspace. Your
  Claude Code settings are not loaded, so settings such as `cleanupPeriodDays` do
  not apply to these runs.
- Use full model IDs such as `claude:claude-sonnet-5`. An alias like
  `claude:opus` works, but pcode cannot look up its context window, so set
  `PCODE_CONTEXT_WINDOW` for compaction. pcode's output limit is passed on as
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, capped at the model's maximum; for a model
  pcode has no limits for, Claude Code's default applies.
- The model sees tool names as `mcp__pcode__<name>`; pcode displays its own names.
- Anthropic server tools (web search, web fetch, code execution) are not
  available, so pcode's local web tools are used.
- Text pcode adds beside tool results (steering, plan reminders, limit warnings)
  reaches the model as a message the user sent while it was working.
- Thinking is shown in summarized form.

## Local Meridian provider

!!! note "Turned off"
    Meridian is off, along with [pcode's own Anthropic sign-in](#sign-in-with-your-anthropic-account):
    `/model` does not offer it and a saved `meridian:` model fails to start with
    a pointer to the matching `claude:` one. The rest of this section describes it
    with `LEGACY_ANTHROPIC_AUTH = True` in `src/pcode/models.py`.

[Meridian](https://github.com/rynfar/meridian) runs Claude Code behind a local
Anthropic-compatible API, so a `meridian:` model uses your Claude subscription.
[Anthropic provider options](https://github.com/aweis89/pcode/blob/master/dev/anthropic-providers.md)
compares it with `/login`.

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

To see thinking, `/show-thinking on` must be set and Meridian must forward
thinking. A private instance does this already. For an external proxy,
`/show-thinking` reports the proxy's setting, and pcode warns once per session
when thinking display is on but the proxy is not forwarding it. Turn on the
passthrough adapter's Thinking Passthrough option on the proxy's `/settings` page
(default <http://127.0.0.1:3456/settings>); it is off by default and affects every
client of that proxy, so pcode leaves it to you. Thinking the model never sent
still cannot be shown.

## Model-only HTTP proxy

Set `PCODE_LLM_PROXY` to send Codex model requests, and only those, through an
HTTP proxy:

```sh
PCODE_LLM_PROXY=http://127.0.0.1:8080 pcode --model openai-codex:gpt-5.6-sol
```

- HTTP and HTTPS proxy URLs work (HTTPS traffic uses CONNECT). Unset or blank
  means no proxy.
- Only `openai-codex:` models use it, including Codex token refresh and the
  worker's model calls. Other providers ignore it, so you can leave it set when
  switching providers.
- The Codex client then ignores other proxy settings, including `NO_PROXY`, and
  environment-based TLS configuration, so use a proxy that tunnels HTTPS without
  needing a custom CA from the environment.
- Exa requests and shell commands keep their normal HTTP setup. pcode does not set
  or change `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY`, so tools may still use
  those if they are set.
- Keep proxy URLs that contain credentials out of prompts and diagnostics.
