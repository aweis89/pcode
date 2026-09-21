# Providers and models

## Authentication

For `openai-codex:`, use `/login openai-codex` to sign in with your ChatGPT
account through Pydantic AI's browser OAuth flow. No Codex CLI is required.
Pcode stores credentials in `codex-credentials.json` under `$PCODE_CONFIG_DIR`,
otherwise `$XDG_CONFIG_HOME/pcode` (default `~/.config/pcode`).
`PCODE_CODEX_CREDENTIALS_FILE` overrides the full path. The file is owner-only,
replaced atomically, and refreshed credentials are saved for future launches.

Pcode's stored login takes precedence. When absent, the existing Codex CLI
`auth.json` remains a read-only fallback (`CODEX_HOME` is honored). A malformed
pcode login reports an error rather than silently switching accounts.
`/logout openai-codex` removes only pcode's login, never the CLI's; new models
then use the CLI fallback if available. The active model retains its cached token.
CLI-fallback token refreshes remain in memory only.

The browser callback uses fixed port 1455; close other login flows using that
port before signing in. On headless machines, the separately installed Codex CLI's
`codex login --device-auth` is an alternative. This provider never falls back to
`OPENAI_API_KEY`.

Model availability still
depends on your account. Authentication failures are displayed without raw
provider bodies or credential values.

For ordinary OpenAI API models, use an `openai:...` string and supply
`OPENAI_API_KEY` through your environment. For Anthropic API models, use
`anthropic:<model-id>` and supply `ANTHROPIC_API_KEY` through your environment.
Use the exact API model ID available to your account; pcode does not remap aliases.
The provider extras below are installed by default, including with `make install`.
Run `make install` again to refresh an existing editable installation after
dependency changes.

## Supported providers

Any `provider:model-id` string accepted by Pydantic AI works with `--model` and
`/model`. The environment variables below are what the provider's own client
reads; the picker checks that the variable is *set* (never its value) to decide
which providers to offer. Catalog: whether the picker suggests model IDs from the
Pydantic AI catalog, or only accepts IDs you type.

| Prefix | Enabled by | Catalog |
| --- | --- | --- |
| `anthropic` | `/login` credential or `ANTHROPIC_API_KEY` | yes |
| `openai-codex` | pcode login, then CLI credential file (`CODEX_HOME` honored) | yes (all OpenAI IDs) |
| `openai`, `openai-chat`, `openai-responses` | `OPENAI_API_KEY` | yes |
| `meridian` | `meridian` on `PATH` or `PCODE_MERIDIAN_BASE_URL` | yes (Anthropic IDs) |
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

Providers with a catalog use the Pydantic AI known-model list, which is a static
snapshot, not an account entitlement list. Providers not in this table (for
example `mistral`, `cohere`, `huggingface`, `litellm`) still work from `--model`
if you install their Pydantic AI extra; the picker does not offer them.
Everything besides Anthropic OAuth and Codex is plain API-key access with no
login flow in pcode: set the variable in your shell before launching.

## Sign in with your Anthropic account

Enter **`/login`** in an idle session to sign in with your Anthropic subscription.
pcode prints the authorization URL, opens `claude.ai` in your browser, receives the
authorization code on a loopback callback, and exchanges it for tokens. A current
Anthropic session in the browser makes this a single approval click. The active
conversation keeps its history and switches to the new credential.

```sh
make install
pcode -m anthropic:<model-id>   # then: /login
```

- PKCE (S256) authorization-code flow against the public Claude Code client, with
  a `http://localhost:54545/callback` redirect. Set `PCODE_OAUTH_CALLBACK_PORT`
  when that port is taken; the callback must be reachable from the browser (over
  SSH, forward it with `ssh -L 54545:localhost:54545`). The callback accepts only
  a code whose `state` matches this sign-in; anything else gets an error page and
  the sign-in keeps waiting. Sign-in times out after five minutes.
- Tokens are stored in `~/.config/pcode/credentials.json` (`XDG_CONFIG_HOME` and
  `PCODE_CREDENTIALS_FILE` are honored), written atomically with owner-only (0600)
  permissions. pcode owns this refresh token: expiry is renewed automatically, five
  minutes early, serialized across pcode processes, and again on a 401. Refreshing
  never blocks the terminal and failures never print bodies or token values.
- **`/logout`** removes the stored credential. Sign-in and sign-out are unavailable
  while a run or queued prompts are active.
- Later launches use the stored login automatically, ahead of `ANTHROPIC_API_KEY`.
  Set `PCODE_ANTHROPIC_AUTH=api-key` to force environment API-key access, or
  `PCODE_ANTHROPIC_AUTH=oauth` to require this login.
- Uses OAuth Bearer authentication with Claude Code beta headers, user agent, and
  system preamble, and sends requests to `https://api.anthropic.com` regardless of
  `ANTHROPIC_BASE_URL`. This authenticates as the public Claude Code client against
  an endpoint scoped to it: compatibility support, not an official third-party OAuth
  integration. Entitlements, quotas, and server behavior can change at any time; the
  supported path remains `ANTHROPIC_API_KEY`.
- No API key is minted, and nothing is written to another tool's credential store.

For OpenAI Codex, `/login openai-codex` uses Pydantic AI OAuth and a separate
pcode-owned credential file; the CLI store is only a fallback.

There is no API-key entry UI; pcode's own credential storage holds only its own
`/login` tokens. For ordinary API-key access, set `ANTHROPIC_API_KEY` in your
environment. Login is unavailable while a run or queued prompts are active.

## Choose a model in the terminal

Use **`/model`** or **Ctrl+L** to open the searchable model picker. Type to filter,
use ↑/↓ to select, and press Enter to apply. Filtering matches both provider and
model names, including joined word prefixes: `anthopus` finds Anthropic Opus,
`codluna` finds Codex Luna, and `opus anth` works too. Escape, Ctrl+C, or Ctrl+L closes the
picker without changing the model or editor draft. For a model not in the catalog,
type its full `provider:model-id` (for example `anthropic:claude-opus-5`).

The picker offers every provider from the [supported providers](#supported-providers)
table whose credentials are configured:

- The current provider is included even when using a custom model ID.
- Anthropic is enabled by a stored `/login` credential or `ANTHROPIC_API_KEY`.
  Detection checks for the stored file's presence only: opening the picker never
  reads pcode's credentials.
- Codex is enabled when its pcode or CLI credential file exists (`CODEX_HOME` is honored).
  Opening the picker checks file presence only, not its contents or validity.
- Every other provider is enabled when its environment variable is set; only
  the variable name is checked, never the value.
- `PCODE_LLM_PROXY` applies only to Codex and does not restrict model selection.

Models are grouped by provider and family, with numeric versions sorted newest
first (Opus 5 before Opus 4.8; 4.10 before 4.9). Filtering preserves that order.
The current model is marked, not pinned above newer versions; undated aliases
precede dated snapshots of the same version. This uses model IDs, not release-date
metadata across different families.

Suggestions come from the installed Pydantic AI catalog (for Codex, all OpenAI
model IDs; for Meridian, Anthropic IDs). Opening the picker makes **no network
requests**. This is not an account-entitlement list: the provider checks model
availability and credentials when you use the model. A typed `provider:model-id`
is accepted for any supported provider, configured or not, so you can point at a
provider whose key you export after launch. If no provider is configured, use
`/login`, set `ANTHROPIC_API_KEY`, or run `/login openai-codex` first.

**Changing models continues the current conversation.** Message history, session ID,
plan, tool panel, usage totals, transcript, and editor draft are preserved. The
saved session records the selected model so resuming uses it too. Model-specific
settings are rebuilt for the selected model. Use `/new` to start over instead.
Selecting the current model is a no-op; `--no-save` still applies, and sessions
are saved lazily on their first prompt. A failed switch leaves the old conversation
intact. This also works from offline preview to start a live conversation without
restarting pcode.

The picker also opens while a run or queued messages are active. Like `/effort`,
the selection applies from the next request: the turn in flight finishes on the
model it started with, and the footer shows `current → next` until the switch
happens. Ctrl+C on the running turn keeps the pending selection.

## Saved model and effort defaults

Selecting a model with `/model` (Ctrl+L) saves it as the default for future
startups once the selection takes effect (immediately when idle, otherwise on the
next request). `/effort` and Ctrl+N/Ctrl+P save the selected reasoning effort for
the current model only, plus that model as the default. Preferences live in
`~/.config/pcode/preferences.json`
(or `$XDG_CONFIG_HOME/pcode/preferences.json` when set), independently of saved
conversations and `--no-save`. Run `pcode` with no model argument to reuse the
saved model; without a saved default it opens the offline preview. Saved effort
applies per model to OpenAI/Codex, Anthropic, and Meridian models, including new
and resumed conversations, so changing effort on one model leaves the others
alone; a model you have never set falls back to the `effort` default.
`/effort default` restores provider-default behavior for the current model. Use `pcode config unset KEY`
to reset an individual default. `--theme-preview` always stays offline.

`-m` / `--model` overrides the saved model for that launch; `--continue` uses the
session's model. Neither changes the saved default by itself.

`-m` / `--model` selects the Pydantic model/provider without remapping either name.
For `openai-codex:`, pcode constructs the native model with one profile override:
explicit prompt-cache breakpoints are disabled. Pydantic AI 2.43.0 advertises them
for this model family, but the subscription endpoint rejects the marker added by
Harness Planning after `write_plan` with HTTP 400. Authentication and streaming
still use the native provider, not a custom transport.

## Reasoning effort

For OpenAI/Codex, Anthropic, and Meridian models, use **Ctrl+N** to increase effort and **Ctrl+P** to
decrease it, or `/effort low|medium|high|xhigh`. `/effort` shows the current
setting; `/effort default` removes the override. Each model remembers its own
level, so raising effort on one model does not raise it elsewhere. Slash completion includes these
values, and the footer shows the selected effort.

Shortcuts stop at the lowest/highest level rather than wrapping. From the
unspecified provider default, they use medium as the starting point (Ctrl+N
selects high; Ctrl+P selects low). Changes apply to the **next turn**, not an
in-progress run, and preserve your draft. Up/Down still navigate history and
completions. Model support varies; not every model accepts every effort level.
Effort overrides are in-memory, survive `/new`, and are not saved with sessions.
On Anthropic and Meridian, `xhigh` uses the native level when supported by the
model profile, otherwise it sends Anthropic’s `max` effort. Older models may not
support effort or the highest level; provider validation still applies. This
control sets effort without changing the model’s thinking configuration.
Preview and other providers do not support this control.

## Local Meridian provider

**Opt-in managed instance (Meridian 1.71.1):**

```sh
pcode config set meridian_managed on
pcode -m meridian:claude-sonnet-5
```

When constructing a Meridian provider, pcode starts one private proxy per pcode
process, on an automatically allocated loopback port with a random API key. It
writes a temporary, private adapter configuration with thinking passthrough on,
uses a separate session directory and empty plugin directory, disables persistent
telemetry and update checks, and checks `/health` plus effective adapter settings
before connecting. Concurrent pcode processes have separate instances. Normal
process exit terminates only the owned proxy and removes its temporary state;
switching models keeps it available until exit. A crashed proxy is not automatically
restarted and requests are not replayed. Startup has a 30-second readiness deadline.

This is **configuration/session isolation, not an authentication sandbox**:
Meridian still uses your existing Claude login and disk-configured Meridian
profiles. pcode does not copy credentials, log you in, install or upgrade Meridian,
or modify your shared proxy. Inherited `MERIDIAN_*` / `CLAUDE_PROXY_*` overrides are
not applied to managed instances. The implementation is gated to the verified
1.71.1 release; other versions can use external mode. Forced termination of pcode
(e.g. `kill -9`) cannot run normal cleanup.

The `meridian_managed` preference defaults to `off` and is saved alongside other
pcode settings. You can also use `/config set meridian_managed on` interactively;
it applies when a Meridian provider is next created (restart pcode to apply it to
an existing Meridian conversation). Use `pcode config set meridian_managed off` or
`pcode config unset meridian_managed` to return to external mode.

An explicit `PCODE_MERIDIAN_BASE_URL` always selects external mode. Otherwise,
`PCODE_MERIDIAN_MANAGED=1` or `0` overrides the saved preference for that process;
an unset or empty variable uses the saved preference. Config commands display the
saved default, not environment overrides. Changing the preference does not stop
an already-owned proxy or reroute active requests.

**Externally managed instance:** Use your running [Meridian](https://github.com/rynfar/meridian) proxy as a separate
provider (no pcode-managed subscription login):

```sh
env -u PCODE_LLM_PROXY pcode -m meridian:claude-sonnet-5
```

The default endpoint is `http://127.0.0.1:3456`. Override it with
`PCODE_MERIDIAN_BASE_URL` (the server root, without `/v1/messages`). If your proxy
requires an API key, supply `PCODE_MERIDIAN_API_KEY` in the environment. Otherwise,
pcode uses a non-secret placeholder. It never inherits `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN`, or `ANTHROPIC_BASE_URL` for Meridian requests; Meridian owns
upstream authentication. Global HTTP proxy settings are ignored by this client.

**`/model` / Ctrl+L** includes Meridian when its executable is on `PATH`, when
`PCODE_MERIDIAN_BASE_URL` is configured, or when the current model is Meridian.
Suggestions use the installed SDK's Claude model catalog; type
`meridian:<model-id>` for other IDs supported by your proxy. Selecting one uses the
normal new-conversation flow. Discovery does not start Meridian or verify model
access; the proxy must already be running.

Requests use the Anthropic streaming API with `x-meridian-agent: passthrough`, so
pcode—not Meridian's built-in agent—executes the supplied tools. There is no
fallback to direct Anthropic requests if the proxy is unavailable.
`PCODE_LLM_PROXY` is ignored when using Meridian; that setting applies only to Codex.

Each request also carries `x-litellm-session-id`, derived from the current pcode
conversation ID. Tool rounds and saved-session resume reuse it; `/new` and
independent delegates get separate identities, even when delegates run in parallel.
For Meridian 1.71.1, telemetry should show `lineage=continuation` on ordinary
follow-up tool rounds. Repeated `independent-request:headerless-tool-result` means
the running client is missing this integration; restart pcode after upgrading
(already-running Python processes do not reload it).

**Thinking visibility:** `/show-thinking on` controls pcode's saved-thinking
scrollback view. Meridian must also forward readable thinking blocks. The opt-in
managed instance enables and verifies **Thinking Passthrough** in its private
configuration. For an external proxy, inspect the **passthrough** adapter's
**Thinking Passthrough** option in its `/settings` page (default:
<http://127.0.0.1:3456/settings>). Meridian 1.71.1 defaults this to off. Changing an
external proxy's adapter affects other clients too, so pcode does not modify it
automatically. Forwarding is separate from enabling model thinking or setting
effort; upstream-omitted thinking still cannot be displayed.

When a spinner is silent, compare Meridian's request telemetry: queue wait,
time to first byte, upstream duration, status/error, and lineage. An early first
byte is not necessarily visible text, and a large output-token count alone does
not prove what happened during the pause. The UI's “Waiting for model…” means no
new displayable event, not necessarily an idle upstream connection.

## Model-only HTTP proxy

Set `PCODE_LLM_PROXY` to route **Codex model requests only** through an HTTP proxy:

```sh
PCODE_LLM_PROXY=http://127.0.0.1:8080 pcode --model openai-codex:gpt-5.6-sol
```

HTTP and HTTPS proxy URLs are supported (HTTPS model traffic uses CONNECT).
This applies to `openai-codex:` models only. Other providers ignore this setting
and retain their normal routing. You can leave it set when resuming a non-Codex
session or switching providers in the model picker.
An unset or blank value preserves the normal provider behavior.

The dedicated model client ignores global proxy settings, including `NO_PROXY`,
when this option is set. Codex token refresh and the explorer's inherited model
calls also use that client. Exa requests and shell subprocesses retain their normal
HTTP configuration: pcode does not set or modify `HTTP_PROXY`, `HTTPS_PROXY`, or
`ALL_PROXY`. If those variables are already set, tools may still use those proxies.
The model client also ignores environment-based TLS configuration (`trust_env=False`);
use a proxy that tunnels HTTPS without requiring a custom environment-specified CA.
Do not include proxy URLs containing credentials in prompts or diagnostics.
