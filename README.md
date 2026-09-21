# pcode

A small, streaming terminal for a Pydantic AI Coder agent, with an offline
UI preview. See [PLAN.md](PLAN.md) for the longer-term direction.

## Install with Homebrew

With [Homebrew](https://brew.sh/) installed:

```sh
brew tap aweis89/pcode https://github.com/aweis89/pcode.git
brew install --HEAD aweis89/pcode/pcode
pcode --theme-preview
pcode -m openai-codex:gpt-5.6-luna
```

This repository doubles as a Homebrew tap. The explicit repository URL is
required because its name is `pcode`, not `homebrew-pcode`. There are no tagged
releases yet, so the formula installs the latest `master` with `--HEAD`, rather
than a stable release. These commands become available once `Formula/pcode.rb`
is published to GitHub.

Homebrew installs Python 3.13 and uses `uv` at build time to install the
application and its locked dependencies into a private environment. Installation
requires network access to fetch Python packages; it does not modify your global
Python environment. Run `pcode` directly after installation (no `uv run` needed).
Provider authentication is still required for live models, as described below.

To update or uninstall:

```sh
brew update
brew upgrade --fetch-HEAD aweis89/pcode/pcode
# To remove:
brew uninstall pcode
brew untap aweis89/pcode
```

The formula includes offline smoke tests: `brew test aweis89/pcode/pcode`.
This is an upstream tap, not a formula in `homebrew/core`.

## Run from source

With [uv](https://docs.astral.sh/uv/) installed, from this directory:

```sh
uv run pcode -m openai-codex:gpt-5.6-luna
```

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

### Prompt cache warnings

For the planning-specific cache issue and why reminders are now append-only, see
[prompt caching and plan reminders](docs/prompt-caching.md). `make cache-report`
summarizes how prompt caching actually performed in saved sessions.

Pcode enables Harness's [cache-bust monitor](https://pydantic.dev/docs/ai/harness/warn-on-cache-busts/)
for the main agent and sub-agents. A `Prompt cache miss` warning appears in the
transcript when cache reads drop below half of an established prefix of at least
1,024 tokens. It includes the model, token counts, and a possible cache-expiry
hint. Warnings survive redraw and saved-session resume; they do not interrupt the run.

The monitor compares requests within each agent run, not across chat turns or
restarts. A sustained collapse warns once until cache reads recover. It stays
quiet if the provider never reports an established cache. This is an observation,
not proof of a prompt bug: compaction, prefix changes, or provider cache expiry
can all cause a miss. No prompt contents are included in the warning.

#### Diagnosing a miss

The token counts alone cannot say *why* a prefix stopped matching, so pcode
fingerprints every model request and keeps a rolling window of the last few. When
a miss fires, the warning gains a one-line diagnosis and the window is written to
`~/.local/state/pcode/cache-diagnostics/` (`XDG_STATE_HOME` is honored):

```
! Prompt cache miss
  anthropic/claude-opus-5: Cache hit collapsed at model request 14: read 11105 ...
  Message 6 of 31 changed (kind request -> request, 8100 -> 240 chars, cache points 0 -> 0). 25 message(s) after it were re-sent.
  Request fingerprints: ~/.local/state/pcode/cache-diagnostics/20260919T035812-4821-step14.json
```

The diagnosis separates the two causes that the token counts conflate:

- **`Prefix intact: ... nothing rewrote history`** — every earlier message was
  byte-identical and the rest were appended. The prompt is stable, so suspect the
  cache TTL (the line reports the gap since the previous request) or where the
  breakpoints landed, which the dump lists as `cache_point_indexes`.
- **`Message N of M changed`** — something rewrote history in place, and the named
  index is the first one that moved. `Instructions changed`, `Tool definitions
  changed`, `Cache settings changed`, and `History shrank` cover the cases that sit
  ahead of, or instead of, a message edit.

The dump holds digests, sizes, part kinds, token counts and breakpoint positions
for each request in the window — never prompt text, which would otherwise leak the
file contents and command output the agent had read. Compare consecutive entries
to see exactly which message moved. Set `PCODE_CACHE_DIAGNOSTICS=off` to disable
the dumps, or to a directory path to write them elsewhere; the warning itself is
unaffected.

### Global configuration

Global defaults are shared across workspaces in `~/.config/pcode/preferences.json`
(or `$XDG_CONFIG_HOME/pcode/preferences.json`). Inspect and edit them without
opening a terminal UI or connecting a model:

```sh
pcode config                     # List effective startup defaults as JSON
pcode config path                # Print the resolved config path
pcode config get theme
pcode config set theme light
pcode config set autocompact on
pcode config set effort high
pcode config set model openai-codex:gpt-5.6-luna
pcode config unset model          # Remove saved model; return to offline preview
pcode config unset theme          # Restore built-in dark theme
pcode config reset                # Remove every saved default at once
```

The same commands are available inside pcode as `/config`, with tab completion:
`/config set theme light`, `/config get autocompact`, `/config unset effort`, etc.
**Config edits affect the next launch, not the running conversation.** To change
an active setting and save its default immediately, use `/theme`, `/effort`,
`/model`, or `/autocompact` instead. CLI overrides such as `--theme` and `--model`
do not rewrite global defaults, and resumed sessions retain their own model.

#### Per-repository overrides

A workspace's `.pcode/preferences.json` is layered over the user file at launch,
so a setting can hold for one repository and be committed for everyone who
clones it. `config list` and `config get` show the merged result;
`config project` edits the repository file (from `-C DIR` or the current
directory):

```sh
pcode config project set worktree on    # this repo only; writes .pcode/preferences.json
pcode config project list               # the raw overlay
pcode config project unset worktree
pcode config project reset              # Drop the whole overlay
```

A cloned repository must not be able to run code or pick credentials on your
behalf, so `project_extensions`, `trusted_projects`, `extension_dirs`,
`meridian_managed`, and `anthropic_auth` are user-only: the project file cannot
set them, and pcode says so at launch if it tries. The overlay is read from the
launch workspace before any worktree is created, so `worktree on` in a
repository's file is what starts each of its sessions in a worktree.

#### Trusting a repository's own code

A repository can ship code that runs at launch with your permissions:
`.pcode/extensions/*.py` (see `/extensions`) and `.pcode/worktree-setup`. Neither
runs until you trust that repository. The first interactive launch inside one
that ships either asks:

```
pcode: /path/to/repo ships code that runs at launch with your permissions: .pcode/worktree-setup
Trust this repository? [y/N]
```

`y` records the repository's primary checkout in `trusted_projects` (so all of
its worktrees are covered) and never asks again; `n` skips the code for this
launch and asks next time. `--print` has nobody to ask, so it skips and says so.
Revoke with `pcode config unset trusted_projects` (or edit the `:`-separated
list). `pcode config set project_extensions on` trusts every repository, which
is only sensible on a machine where you wrote all of them.

| Key | Built-in default | Values |
| --- | --- | --- |
| `theme` | `dark` | `dark`, `light`, `auto` |
| `syntax_dark` | `gruvbox-dark` | A Pygments style for fenced code on the dark palette |
| `syntax_light` | `gruvbox-light` | A Pygments style for fenced code on the light palette |
| `autocompact` | `off` | `on`, `off` |
| `code_mode` | `off` | `on`, `off` (batch read-only tools through a sandboxed `run_code`) |
| `tool_output_mode` | `spill` | `spill`, `truncate`, `off` |
| `tool_output_threshold` | `10000` | Positive integer, characters that trigger reduction |
| `tool_output_preview_chars` | `1000` | Positive integer, spill preview characters |
| `tool_output_max_chars` | `4000` | Positive integer, truncation budget (also spill-failure fallback) |
| `tool_output_strategy` | `head_tail` | `head`, `tail`, `head_tail` (truncation only) |
| `tool_output_retention_hours` | `0` | Whole number, spill retention; `0` keeps indefinitely |
| `meridian_managed` | `off` | `on`, `off` (private local Meridian proxy) |
| `repo_context_walk_up` | `on` | `on`, `off` (inherit ancestor instruction files) |
| `repo_context_nested` | `off` | `off`, `pointer`, `contents` (discover instructions on file-tool traversal) |
| `skill_commands` | `prefix` | `prefix`, `bare`, `both`, `off` (how discovered skills appear as slash commands) |
| `skill_dirs` | `~/.agents/skills:.agents/skills` | `:`-separated directories searched for skills; relative entries resolve against the workspace |
| `worktree` | `off` | `on`, `off` (start each new session in its own `.worktrees/` git worktree) |
| `worktree_exit` | `ask` | `ask`, `merge`, `keep` (what to do with unmerged commits when a session worktree is left) |
| `project_extensions` | `off` | `on`, `off` (`on` trusts every repository's `.pcode/extensions` and `worktree-setup`) |
| `trusted_projects` | `` | `:`-separated repository paths whose shipped code may run; the launch prompt appends here |
| `effort` | `default` | `low`, `medium`, `high`, `xhigh`, `default` (OpenAI/Codex, Anthropic, Meridian); fallback for models `/effort` has not set |
| `model` | `null` (offline preview) | A model name, normally `provider:model` |

#### Code highlighting styles

Fenced code keeps its own background, so each palette gets its own Pygments
style: `syntax_dark` applies whenever the resolved theme is dark, `syntax_light`
whenever it is light. `/syntax NAME` changes the style for the palette in use and
saves it as that palette's default; `/syntax` alone reports the current one. Tab
completion lists the styles, and an unknown name is rejected with the full list.
`/theme-preview` renders each of them on one line, marks the one in use, and
repeats the commands below, so a style can be chosen by eye rather than by name.

The completion menu and the prompt chrome (the chevron, plan rows, the frame,
`@file` references) are painted from the same style, so the screen matches the
code on it. A Pygments style only colors code, though, so any color it leaves
out or that would be unreadable falls back to the palette's own. The two are
judged against different backgrounds: the menu brings the style's own surface
with it, while chrome lands on the terminal's background, so a light style
chosen while the dark palette is active keeps its popup but leaves the chrome
on the palette. `/colors terminal` drops the style entirely and both return to
the palette.

These are the styles Pygments installs here; a Pygments style plugin package adds
to the list automatically.

Darker backgrounds: `coffee`, `dracula`, `fruity`, `github-dark`, `gruvbox-dark`,
`inkpot`, `lightbulb`, `material`, `monokai`, `native`, `night-owl`, `nord`,
`nord-darker`, `one-dark`, `paraiso-dark`, `rrt`, `solarized-dark`, `stata-dark`,
`vim`, `zenburn`.

Lighter backgrounds: `abap`, `algol`, `algol_nu`, `arduino`, `autumn`, `borland`,
`bw`, `colorful`, `default`, `emacs`, `friendly`, `friendly_grayscale`, `igor`,
`lilypond`, `lovelace`, `manni`, `murphy`, `paraiso-light`, `pastie`, `perldoc`,
`rainbow_dash`, `sas`, `solarized-light`, `staroffice`, `stata-light`, `tango`,
`trac`, `vs`, `xcode`.

Styles differ in how much they color: some leave plain identifiers and
punctuation at the default foreground, so a snippet that is mostly names can look
unhighlighted even though the lexer ran. Compare a few against your own terminal
background before settling on one.

`/colors terminal` ignores both settings and uses `ansi_dark` / `ansi_light`
instead, which follow the terminal's own sixteen colors. In that mode
`/theme-preview` lists the style names without samples: drawing them would need
the RGB colors that mode exists to avoid.

Outside the editor, `pcode config set syntax_dark NAME` and
`pcode config set syntax_light NAME` save the same two settings.

Automatic compaction still requires a known context window; setting its global
preference does not validate a particular model or trigger a compaction. For custom
deployments, use `PCODE_CONTEXT_WINDOW` as described below. `/colors` / `--color-style`
remain session-only; MCP configuration and credentials are separate from these
non-secret defaults.

Writes are atomic and serialized across terminals. Unknown JSON keys are preserved;
invalid setting values fall back to built-in defaults. Normal startup tolerates a
malformed file, but config commands report it and refuse to overwrite it: use
`pcode config path` to find and repair it first. Invalid commands exit nonzero.

The current directory is the Coder workspace; select another repository with `-C`:

```sh
uv run pcode -m openai-codex:gpt-5.6-luna -C /path/to/repo
```

For a bare `pcode` command available outside this project:

```sh
uv tool install --editable .
pcode -m openai-codex:gpt-5.6-luna -C /path/to/repo
```

Try asking: `What does this repository do? Read the README and cite relevant files.`
Live conversations save automatically when the first model prompt is submitted.

A prompt on the command line is sent as the first message, then the editor opens
as usual. Add `-p`/`--print` to skip the editor: the reply goes to stdout, tool
activity and errors go to stderr, and the exit status reports whether the turn
succeeded. On a terminal the reply is rendered Markdown, block by block as each
response settles; redirected to a file or a pipe it is the Markdown source,
streamed as it arrives. Without a prompt argument, `--print` reads one from stdin.

```sh
pcode "Summarize the open TODOs in this repo"          # first message, then interactive
pcode -p "Which files handle sessions?" > answer.md    # non-interactive
git diff | pcode -p --no-save                          # prompt from stdin
pcode -p --continue "And the tests for those?"         # continue this directory's latest session
```
Opening the app, using commands, or quitting without a prompt creates no session.
`/new` resets context without deleting the old conversation; its replacement is
created on the next model prompt.

### Shell completion

`pcode --completions SHELL` prints a completion script for `zsh`, `fish`, or
`bash`. It is generated from the argument parser itself, so flags and their
choices (themes, color styles, shells) stay in step with the installed version;
regenerate after upgrading.

```sh
pcode --completions zsh > ~/.zsh/completions/_pcode   # directory must be on $fpath
pcode --completions fish > ~/.config/fish/completions/pcode.fish
echo 'eval "$(pcode --completions bash)"' >> ~/.bashrc
```

The zsh script works either autoloaded from `$fpath` or sourced from `.zshrc`.

### Authentication

For `openai-codex:`, use an existing subscription login. If missing or expired:

```sh
codex login
```

Pydantic reads the CLI's credential store (`CODEX_HOME` is honored); pcode never
prints, copies, or writes it. This provider does not fall back to `OPENAI_API_KEY`.
Refreshed credentials live only in the provider's memory with the default loader,
so you may need to sign in again after restarting. Model availability still
depends on your account. Authentication failures are displayed without raw
provider bodies or credential values.

For ordinary OpenAI API models, use an `openai:...` string and supply
`OPENAI_API_KEY` through your environment. For Anthropic API models, use
`anthropic:<model-id>` and supply `ANTHROPIC_API_KEY` through your environment.
Use the exact API model ID available to your account; pcode does not remap aliases.
The provider extras below are installed by default, including with `make install`.
Run `make install` again to refresh an existing editable installation after
dependency changes.

### Supported providers

Any `provider:model-id` string accepted by Pydantic AI works with `--model` and
`/model`. The environment variables below are what the provider's own client
reads; the picker checks that the variable is *set* (never its value) to decide
which providers to offer. Catalog: whether the picker suggests model IDs from the
Pydantic AI catalog, or only accepts IDs you type.

| Prefix | Enabled by | Catalog |
| --- | --- | --- |
| `anthropic` | `/login` credential or `ANTHROPIC_API_KEY` | yes |
| `openai-codex` | `codex login` credential file (`CODEX_HOME` honored) | yes (all OpenAI IDs) |
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

### Sign in with your Anthropic account

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

For OpenAI Codex, continue to use `codex login`.

There is no API-key entry UI; pcode's own credential storage holds only its own
`/login` tokens. For ordinary API-key access, set `ANTHROPIC_API_KEY` in your
environment. Login is unavailable while a run or queued prompts are active.

### Choose a model in the terminal

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
- Codex is enabled when its CLI credential file exists (`CODEX_HOME` is honored).
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
`/login`, set `ANTHROPIC_API_KEY`, or run `codex login` first.

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

### Local Meridian provider

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


### Model-only HTTP proxy

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

### Web search

The coder can search the web and read pages. Search and page fetching are
separate because provider-native search returns snippets only; reading
documentation needs the fetch either way. Each picks the best backend available:

| | Search | Fetch a URL |
| --- | --- | --- |
| Model has a native tool (Anthropic, OpenAI) | provider runs it server-side | Anthropic runs it server-side |
| `EXA_API_KEY` set | Exa `web_search` | Exa `get_page` |
| Otherwise | DuckDuckGo `web_search` | HTTP fetch `get_page`, converted to Markdown |

Native tools are billed by the provider per search; the Exa key is read by the
Exa client and never passed to the model. Search returns up to five results;
page retrieval returns up to 10,000 characters. Queries, URLs, and returned
content go to whichever backend is in use, reach the model, and can be saved in
session history. The explorer sub-agent receives no web tools.

```sh
pcode config set web_search local   # Never advertise native tools to the model
pcode config set web_search off     # No web tools at all
pcode config unset web_search       # Back to auto
```

`local` is the escape hatch for an endpoint that rejects server-side tools.
Changes apply on `/reload` or the next launch. This is all one bundled
extension, `web_research`; copy `src/pcode/extensions/web_research.py` to
`~/.config/pcode/extensions/web_research.py` to change backends, limits, or
instructions, or leave its `setup` empty to remove the tools.

### Browser (per conversation)

`/browser launch` gives the model your installed Chrome, through Harness's
[Playwright tools](https://pydantic.dev/docs/ai/harness/playwright/): navigate,
click, type, snapshot, screenshot, and the rest, plus `browser_open()`, which
brings the window to the front, and `browser_tabs()`. When a page needs you to
sign in, the model leaves it on screen and asks; log in there and tell it when
you are done. A `browser` sub-agent shares the same window, so a multi-step
task can run without every page landing in the main context. `/browser off`
quits that Chrome and removes the tools; a fresh pcode starts with them off.

Chrome is started by pcode with a debugging port and its own profile under
`~/.local/state/pcode/chrome`, apart from your everyday one, and driven over CDP.
That is what lets Google and similar sign-in pages accept it: Playwright's own
Chromium launches flagged as automated and they refuse it. The profile persists,
so a site you log in to once stays logged in for later pcode sessions; delete
the directory to forget everything. Set `PCODE_BROWSER_CHROME` to pick the
binary. With no Chrome installed it falls back to Playwright's Chromium,
downloaded on first use.

`/browser attach` joins the Chrome you already have open instead, logins
included, so nothing needs signing in to. The model works in a tab of its own,
and `browser_tabs()` shows it what you have open, so "check my email" finds the
mail tab and opens that site rather than guessing. Chrome only exposes itself
once remote debugging is on: the first `/browser attach` opens
`chrome://inspect/#remote-debugging` in your Chrome for you to flip the switch,
then run it again. (Starting Chrome with `--remote-debugging-port` works too.)
pcode finds the port from Chrome's `DevToolsActivePort` file, or from
`PCODE_BROWSER_CDP_URL` / `PCODE_BROWSER_PORT_FILE`. pcode opens its own tab
there and closes it on `/browser off`, never quitting your Chrome. This is the
higher-risk mode: the model can act as every account that browser is signed in
to.

| `/browser …` | Does |
| --- | --- |
| `launch` | Open pcode's own Chrome window, with its own persistent logins |
| `attach` | Join the Chrome you have open, your logins included |
| `off` | Close the browser (or pcode's tab in yours) and remove the tools |
| `status` | Show which browser is in use and where it is |

The window is visible and localhost is reachable, since a dev server is the
usual target. The trade-off of turning it on at all: any page the model reads
can tell it to act with your login, and nothing enforces otherwise beyond you
watching the window. This is the bundled `browser` extension; a user file of the
same name replaces it.

### Code mode (opt-in)

[Code mode](https://pydantic.dev/docs/ai/harness/code-mode/) replaces individual
tool calls with a single sandboxed Python snippet, so the model can fan out
lookups with `asyncio.gather`, filter results, and return only what matters
without a model turn per dependent batch.

```sh
pcode config set code_mode on   # Applies on next launch
pcode config unset code_mode    # Back to plain tool calling
```

Only read-only lookups are sandboxed: `read_file`, `list_files`, `grep`,
`read_tool_result`, `web_search`, and `get_page`. Edits, plan updates, the
persistent shell, and delegation keep issuing their own tool calls, so diffs,
command previews, and the plan panel still show what happened rather than an
opaque snippet that did it. A `run_code` call is displayed by the calls the
snippet makes and its size (`grep · read_file ×2 · 12 lines`); the snippet
itself is visible in the tool-call inspector.

The snippet also streams into the pinned preview box as the model writes it,
titled `Preparing code · not yet run`, in the same place edit diffs and command
output appear. Only complete lines are shown, the box clears the moment the
snippet is dispatched, and the text never enters the transcript — it is a
pending argument, not a result. `/show-edits off` hides it along with edit previews.

Snippets run in the Monty sandbox with no host filesystem or environment of their
own: pcode passes no `mount` or `os_access`, so the only way out is the sandboxed
tools, which enforce the same workspace rules as ever. Harness caps each snippet
at 30 seconds and 256 MiB of heap.

### Tool permissions

**Live mode enables actual Coder file edits and shell tools. There is no approval
UI.** pcode does not implement its own permission model and does not use prompt
text as a safety control. Permission management is out of scope: run pcode inside
a sandboxing wrapper (a container, VM, or an OS sandbox such as `sandbox-exec`
or `bwrap`) when you need enforcement, and otherwise use a trusted repository and
a safe working environment.

File tools accept absolute paths anywhere the OS permits, including external
worktrees and temporary directories. Relative paths (including `..`) always use
the selected workspace as their base, even after a shell command changes its
working directory. `list_files` and `grep` default to the workspace and return
workspace-relative paths, including `../` paths for external results. Protected
patterns such as `.git/*`, `.env`, `.env.*`, `*.pem`,
`*.key`, and `**/secrets*` remain read-only through file tools at any depth.

The explorer subagent has read-only file tools plus the same unrestricted shell
capability as the parent, for inspection, Git queries, and safe tests. Its no-edit
rule is an instruction, not an enforced permission boundary. Shell commands can
read or modify anything the OS allows, including files protected by file tools.
Repository instruction discovery remains scoped to the selected workspace and
its configured ancestors, not every external path the tools can access.

Harness is pinned to upstream commit
[`12bce878da99bca61a5d8d798bff0a3bc93bd153`](https://github.com/pydantic/pydantic-ai-harness/commit/12bce878da99bca61a5d8d798bff0a3bc93bd153),
which is newer than the 0.31.0 release. The pin is a direct dependency, so both
`uv sync` and `make install` use it. Coder supplies `read_file`, `write_file`,
`edit_file`, `list_files`, `grep`, and `shell`; pcode adds planning, the explorer,
and optional web search. `list_files` and `grep` use the bundled ripgrep and
respect ignore rules. Edits support either one replacement pair or a
`replacements` array, validated before a single write.

The default upstream `shell` accepts unrestricted commands: treat it as arbitrary
code execution as the invoking user. Foreground calls wait up to 270 seconds
(or a shorter requested timeout), then return the PID and output/status paths
without killing a still-running command. Background mode returns those handles
immediately. Read the returned files to inspect progress and use the returned
process-group stop command to terminate it. Processes and raw output logs can
outlive the turn and pcode itself; `--no-save` does not disable these logs.
Cancelling a call while it is waiting terminates its process group, but cancelling
a later turn does not stop a command whose handles were already returned.
Files and code returned by tools are sent to the selected model.

### Repository instructions (`AGENTS.md` / `CLAUDE.md`)

Both the main agent and explorer load workspace-local `CLAUDE.md` and `AGENTS.md`.
Two independent settings control additional discovery:

```sh
pcode config set repo_context_walk_up off   # Only workspace-local files at startup
pcode config set repo_context_walk_up on    # Also inherit ancestors (default)
pcode config set repo_context_nested pointer  # Notify the agent of nested files
pcode config set repo_context_nested contents # Inject nested instruction contents
pcode config set repo_context_nested off      # Disable nested discovery (default)
```

Use the same commands with `/config` inside pcode. These are saved global defaults,
read when an agent is created (including on launch, resume, or model replacement),
not live toggles for an existing agent. Restart pcode to reliably apply changes.
`pcode config unset KEY` restores that setting's built-in default. Disabling both
still loads workspace-local instructions; neither setting restricts explicit file
reads or removes instructions from saved conversation history.

**Upward walk:** When enabled, for a workspace under your home folder, discovery
stops at home (inclusive); elsewhere, it stops at the filesystem root. Paths are
resolved before walking, so symlinked workspaces inherit from their real ancestors.
A `.git` directory does not stop the walk. Instructions are loaded broadest-first,
workspace-last. Within each directory, `CLAUDE.md` comes before `AGENTS.md`; both
load when their contents differ. Harness deduplicates files by resolved path and
content, keeping the first occurrence. The startup banner lists the files actually
loaded without printing their bodies. Files are cached within each agent run and
reread for the next run.

**Nested discovery:** This works with the upward walk either on or off. After a
successful `read_file`, `list_files`, or `grep` tool call within the workspace,
pcode's Harness context adapter checks the accessed file's directory or the
selected search directory. `pointer` adds a
note telling the agent to read its instruction file if relevant; `contents` adds
the instruction body to the conversation. These notes do not change the startup
instruction prefix. Each directory is surfaced at most once per run. Unlike
startup loading, Harness selects only the first matching filename in that
directory (`CLAUDE.md` before `AGENTS.md`). It does not recursively scan the tree
or check intervening directories when jumping directly to a deeper file. Shell
commands, writes, and edits do not trigger this discovery.

The `.claude`, `.agents`, `.codex`, and `.grok` asset inventory remains
workspace-local and metadata-only; it does not load asset bodies or execute hooks.
It goes to the model rather than the startup banner, which already lists the
skill commands. Discovered instructions are sent to the selected model, so review
inherited and nested files when working in a shared directory tree.

### Skills as slash commands

Every `SKILL.md` found under those asset roots becomes a command, so a skill can
be invoked deliberately instead of hoping the model notices it. A skill in
`.claude/skills/cache-report/SKILL.md` is named after its directory:

```
/skill:cache-report check the last session
```

The command sends a normal message asking the model to read that file and follow
it, with anything you type after the command appended. It is queued like a typed
message, so send mode, steering, and Ctrl+C behave as usual. The skill body is
not preloaded into the prompt; the model reads the file with its own tools.

`skill_dirs` adds directories searched after the asset roots, so skills can live
outside the workspace:

```sh
pcode config set skill_dirs '~/.agents/skills:.agents/skills'   # default
pcode config set skill_dirs '~/.agents/skills:/opt/team/skills' # share a checkout
pcode config set skill_dirs ''                                  # asset roots only
```

Entries are separated by `:`, `~` expands to your home directory, and a relative
entry resolves against the workspace. Each directory is searched recursively for
`SKILL.md`. Asset roots are scanned first and a duplicated name keeps the first
match, so a workspace skill shadows a user-level one. Skills found outside the
workspace are referenced by absolute path.

Naming follows `skill_commands`: `prefix` gives `/skill:NAME` (default), `bare`
gives `/NAME`, `both` registers the bare name as an alias of the prefixed one,
and `off` registers nothing. A bare name that collides with a built-in command is
dropped, and the built-in wins. Discovery happens at launch, so add a skill (or
change this setting) and restart to pick it up. The startup banner lists the
commands that were registered. Only the frontmatter `description` is read at
launch, to label the completion menu.

### One git worktree per session

Several sessions editing one checkout trample each other: one session's
`git checkout` or `stash` eats another's uncommitted edits. pcode can give each
session its own worktree and make that the workspace, so file tools, the shell,
repository instructions, and the saved session all point there. The model needs
no instructions and relative paths cannot land in the mainline by mistake.

```sh
pcode --worktree                      # .worktrees/pcode-<session-id-prefix>, branch of the same name
pcode --worktree fix-thing            # .worktrees/pcode-fix-thing
pcode config project set worktree on  # default for this repository (committed in .pcode/)
pcode config set worktree on          # default for every git repository
pcode --no-worktree                   # stay in the current checkout this once
```

The worktree lives under `.worktrees/` in the primary checkout (added to
`.git/info/exclude`, so `git status` stays clean without touching `.gitignore`)
and branches from the mainline's current branch, reusing a branch of that name if
one exists. Session worktrees and their branches are always prefixed `pcode-`, so
`git worktree list` and `git branch` show which ones pcode made; `make worktree`
style invocations of `python -m pcode.worktree` use the name as given. Starting pcode inside an existing worktree, outside git, or with
`--continue` never creates another one. Resuming a session (`pcode -c`, or
`pcode -C .worktrees/NAME -c` from elsewhere) lands back in its worktree because
the workspace is what the session saved.

Git cannot install dependencies or copy untracked config, so after checkout pcode
runs two optional scripts inside the new worktree, each with `PCODE_MAIN`,
`PCODE_WORKTREE`, and `PCODE_BRANCH` set:

| Script | Runs |
| --- | --- |
| `~/.config/pcode/worktree-setup` | always (for what every repo needs: `direnv allow`, copying `.envrc`) |
| `<repo>/.pcode/worktree-setup` | only in a trusted repository (launch prompt, or `project_extensions on`), since it is code shipped with the repo |

An executable script runs directly (give it a shebang); anything else runs
through `sh`. A non-zero exit aborts the launch and removes the half-made
worktree. This repository's own script symlinks the shared `tmp/` cache and runs
`uv sync`, because the editable install records an absolute `src/` path and a
shared `.venv` would silently import the other checkout.

Inside the session, `/worktree` shows the branch and what is unmerged,
`/worktree merge` merges the mainline branch into the worktree (so conflicts are
resolved there, never in the mainline checkout) and then fast-forwards the
mainline, `/worktree finish` does that and then removes the worktree and its
branch and quits, `/worktree remove` deletes the directory once it is merged
and clean, and `/worktree list` shows every worktree. Nothing is ever forced.

`/worktree clean` sweeps up the leftovers: every other worktree of the
repository with nothing uncommitted, nothing untracked, and nothing the mainline
branch lacks is removed along with its branch. Anything else is listed with the
reason it was kept, so the command cannot lose work. It runs from the mainline
checkout too (`make worktree-clean`), which is usually where the pile is
visible. A worktree someone locked with `git worktree lock` is skipped.

When a merge stops on conflicts it says which files, and `/worktree resolve`
hands them to the model: it gets the branch names and the conflicted paths and
is asked to resolve each so both sides survive, run the tests, and commit the
merge (never abort it). Run `/worktree merge` or `finish` again afterwards. The
merge is never started for you, and the model is never asked without you typing
the command; a conflict at exit prints the resume command and that same hint.

Leaving a session tidies its own worktree (one pcode made, prefixed `pcode-`;
hand-made ones are only reported):

| State on exit | What happens |
| --- | --- |
| Untouched: clean, nothing unmerged | Removed with its branch, no question. A session that never had a turn is deleted too; otherwise it is repointed at the mainline so `pcode -c` still works. |
| Committed but unmerged | `worktree_exit`: `ask` (default) prompts `Merge and remove the worktree? [Y/n]`; `merge` does it silently; `keep` leaves it. A merge that conflicts or cannot fast-forward keeps everything and prints how to resume. |
| Uncommitted changes | Kept, with the resume command. Committing on your behalf at exit is not pcode's call. |

`--print` has nobody to ask, so it only does the untouched cleanup. Merging
never pushes; push from the mainline when you are ready.

## Sessions and debugging

```sh
uv run pcode --sessions
uv run pcode --continue                             # this directory's newest session
uv run pcode --continue SESSION_ID
uv run pcode -m openai-codex:gpt-5.6-sol --no-save  # opt out for a sensitive session
```

`-c` / `--continue` accepts an unambiguous ID prefix (at least 8 characters) and
restores the saved model, workspace, and structured message history. Without an ID
it picks the newest session whose workspace is the current directory (or `-C`), not
the newest session overall. It prints recent transcript blocks and waits for your
next message; it does not automatically re-run tools.
A different explicit `-m` or `-C` is rejected on resume. Only one process may open
a session for writing. `/resume` opens a full-screen browser of saved conversations
in the current repository, including its linked worktrees (newest first, labeled
by their first prompt), or the exact workspace outside Git, with every
prompt and a truncated, rendered response for the selected session alongside. `/`
searches prompts across sessions (space-separated words are all required) and ↑/↓
move the selection while you type; `r` includes responses, `w` includes every
workspace. Tab focuses the content pane, where arrows scroll by line,
PageUp/PageDown by page, and Ctrl+U/Ctrl+D by half a page. Enter resumes the selected
session in place, Esc cancels. Resuming restores the saved model, history, and plan.

The bundled `session_history` extension lets the model answer questions such as
“What did we decide about editor flicker?” without resuming another session.
`search_sessions` returns ranked excerpts with session/turn IDs, dates, outcomes,
and active/inactive branch labels. `read_session` retrieves a referenced turn,
paginates long text, and includes bounded ancestor context (not sibling branches).

- Default `scope="project"` includes linked worktrees. `workspace` restricts to
  the exact directory; `all` is for explicitly cross-project questions.
- `scope="session"` searches the current conversation, including original turns
  dropped from model context by compaction. It reads the journal without taking
  the live session's lock. This is not a replacement for model checkpoints.
- Search covers saved prompts, consumed steering messages, assistant text, and
  tool summaries/commands, not full tool results, reasoning, or unsaved conversations.
  Steering messages from older versions were not journaled and are not recalled.
  Historical claims and
  failed or abandoned attempts are evidence, not proof that a change shipped.
- New sessions record their project path so deleted worktrees remain discoverable.
  Older sessions use Git discovery or the conventional `.worktrees/` layout;
  a deleted legacy worktree elsewhere may need `scope="all"`.

Keyword retrieval is BM25 (the same ranking as Harness's `ConversationSearch`)
with a bonus for an exact phrase match; it needs no credentials or network.
Optional hybrid retrieval uses
Pydantic AI's `Embedder` when you explicitly set a model before launching:

```sh
PCODE_HISTORY_EMBEDDING_MODEL=openai:text-embedding-3-small pcode
```

This opts into sending redacted chunks and queries to the selected embedding
provider, using that provider's normal credentials. Redaction is best-effort;
do not enable a hosted model for history you cannot send off-machine. Local
embedding models require their provider's optional dependencies. No model is
selected automatically, and `semantic=false` on a search forces keyword-only.
Provider/cache failures fall back to keywords with a warning.

The optional cache is `.history-embeddings.sqlite3` inside the session root,
mode 0600, containing content hashes and vectors, not transcript text. Vectors
are still sensitive data. It is created lazily; there is no startup indexing.
Each search has a 64 MiB journal-read budget, returns candidates from at most
10,000 chunks, and embeds at most 128 new chunks. Sessions are scanned newest
first, each journal from its beginning. Reaching a limit reports partial coverage;
branch labels are unknown when the rest of a journal was not read. Narrowing to
`scope="session"` avoids spending the budget on other conversations. Reading a
referenced turn also has a 64 MiB scan budget. Subsequent semantic searches extend
the vector cache (they do not advance the journal-read window). Cache
keys include the model and content; removed sessions are never returned, but
old cached vectors remain until the cache is deleted. Delete that file with
pcode stopped to clear it (also necessary if a provider changes a model's vector
dimensions under the same name).

To disable recall, create `~/.config/pcode/extensions/session_history.py` with
`def setup(pcode): pass`, then `/reload`. The tools honor the same session-storage
configuration as the terminal.

Default location: `$XDG_STATE_HOME/pcode/sessions`, or
`~/.local/state/pcode/sessions`. Override with `--session-dir PATH` or
`PCODE_SESSION_DIR`. Each session directory contains:

- `session.json`: model, workspace, timestamps, completed-turn usage, and package versions.
- `steps.sqlite3`: Harness `StepPersistence` events, full Pydantic message snapshots
  (including tool arguments/results and provider reasoning metadata), and a tool-effect ledger.
- `transcript.jsonl`: submitted prompts, streamed text, completed blocks/tool summaries,
  bounded redacted tool-inspection arguments/results, and structured failure diagnostics (HTTP status, provider code/parameter/message).

Session directories are mode 0700 and data files are 0600. **These files contain
conversation and repository content in plaintext.** They stay outside the repo by
default; do not commit or share them without inspection. No HTTP headers, provider
credential store, or auth tokens are deliberately captured. Error diagnostics
redact known token formats, credential assignments, and credential values from
the environment; this is best-effort, not a guarantee that arbitrary sensitive
text can be recognized. Model snapshots retain their content faithfully for replay,
so sensitive material pasted by you or returned by a tool can still be stored.
Use `--no-save` when that is inappropriate. Delete a closed session's directory
to remove it; there is no automatic retention policy yet.

Checkpoints are saved by Harness at settled tool boundaries, not just when an
answer succeeds. If the request after a completed tool fails, its tool result is
retained for the next turn and for resume. Interrupted streaming text is retained
in the journal, even when it cannot become a safe model checkpoint. Resume uses
the most recent settled checkpoint without requiring review of interrupted tools.
Pending tool calls are not automatically replayed, and their unknown outcomes
remain in the diagnostic ledger. Interrupted tools may already have changed the
workspace; resuming does not undo those effects. Checkpoints do not restore files,
running processes, or capability-local state such as the in-memory planner.

Dropped provider connections and transport timeouts get one automatic retry by
default (two attempts total per submitted turn). The retry reuses the failed
request's checkpoint, including completed tool results, without adding a
"continue" prompt. Partial streamed output may remain visible but is excluded
from the retried request. Authentication, HTTP status errors, tool errors, and
user cancellation are not automatically retried by pcode. Anthropic SDK request
retries are disabled for all three authentication modes: rate-limit, billing,
and server errors surface immediately rather than waiting through hidden
backoff. Transport retries show the failure and attempt count. OAuth credential
refresh still handles an expired access token. Other provider SDKs may retry
internally. Use `/resend` to retry a failed request when ready.

```sh
pcode config set retry_attempts 3   # Three extra attempts per turn, next launch
pcode config set retry_attempts 0   # Disable automatic retries
```

A tool call whose arguments fail the tool's schema is a separate budget. The
model is told what was wrong and gets three corrections by default; past that
the turn ends, naming the tool and the rejected field rather than blaming the
provider. Nested arguments such as `edit_file`'s `replacements` array are the
usual cause, and the correction costs a round trip where the old limit of one
cost the turn. Output validation keeps the stricter single retry.

```sh
pcode config set tool_retries 5   # More corrections before a turn is abandoned
pcode config set tool_retries 0   # Fail on the first rejected tool call
```

Use `/resend` while idle to try again manually without adding another user
message. The original prompt appears above the task bar with the normal running
spinner. Completed tool results stay in context; if the last answer completed,
only that final response is regenerated. An empty conversation cannot be resent.
If cancellation left tool effects unsettled, `/resend` refuses to replay them;
inspect the tools and send an explicit next step instead.

Nothing from sessions run before this feature was installed can be reconstructed
from disk; those earlier conversations were memory-only.

### Resource profiling

Use `pcode --profile /tmp/pcode-resources` to sample pcode and its child processes'
CPU, resident memory, and thread counts while reproducing a resource problem.
Quit normally to write `summary.json`; `resources.jsonl` is flushed as it runs.
The destination must be a new directory. Nothing is collected by default.

For a short detailed capture, add `--profile-cpu` (function CPU time across Python
threads) or `--profile-memory` (Python allocation locations and traced memory peaks).
These add substantial overhead, so use separate runs and resource-only captures for timing.
Captures have private permissions, but can contain local source paths; inspect
before sharing. See [profiling and the optimization plan](docs/profiling.md) for
commands, limitations, the offline benchmark, and initial measured hotspots.

Replay saved sessions through the current renderer, without calling models,
rerunning tools, modifying sessions, or printing conversation contents:

```sh
pcode-benchmark --recent 5 --repeat 3 --render-mode both
pcode-benchmark --replay latest --profile /tmp/pcode-replay --profile-cpu
```

Each JSON result reports CPU time, event counts, and expensive attempt ordinals.
`both` compares normal streaming with an experimental end-only renderer; it does
not change normal pcode behavior. This measures historical input on current code,
not the original session's CPU, live prompt redraws, or external processes.

## Offline preview and commands

```sh
uv run pcode                 # no model, canned replies only
uv run pcode --theme-preview # print a sample and the style gallery, then exit
uv run pcode --theme light   # light input palette
uv run pcode --theme auto    # detect terminal background at startup
```

Type `/` to open the command menu, then narrow it by typing. Use Tab or the
arrow keys to choose. Enter accepts a selected completion; another Enter runs it.

Type `@` anywhere in a prompt to reference a workspace file. The menu matches
any part of the path, so `@ui.py` finds `src/pcode/ui.py`; files whose name
matches come first, and each row shows the file's size. Accepting a match
replaces `@…` with the workspace-relative path, `./src/pcode/ui.py`, which is
the form the model's file tools take, so it can read or search the file without
guessing where it lives (a name containing spaces is quoted). References are
underlined in the editor. The candidate list comes from `git ls-files` (tracked
plus untracked, honoring `.gitignore`), or a directory walk that skips hidden
and build directories outside a Git checkout, and is refreshed at most every ten
seconds.

The path is all that is sent: pcode never reads a referenced file for you, so
the model decides whether reading it is worth a call. The menu shows each
candidate's size so that cost is visible before you pick.

- `/theme-preview`: fictional Markdown, code, diff, table, and tool summaries,
  followed by a gallery of every installed Pygments style with the commands that
  select one. Never calls the model, even in live mode, and does not enter its
  conversation history. `--theme-preview` (formerly `--demo`, still accepted)
  prints the same thing without a terminal.
- `/theme light`, `/theme dark`, or `/theme auto`: change the input and future output palette.
  Auto uses the terminal background detected at startup with an OSC 11 query,
  falling back to `COLORFGBG`, then dark when unavailable (including redirected
  output). Restart pcode after changing your terminal background. Save auto mode
  with `/theme auto` or `pcode config set theme auto`; the built-in default remains dark.
  `/theme` alone toggles. By default, Rich headings, links, quotes, inline code,
  and tables follow this palette; fenced code uses the palette's own Pygments
  style, `gruvbox-dark` or `gruvbox-light`. Normal body text and the overall
  background remain terminal-native.
- `/syntax NAME`: change the Pygments style for fenced code, the completion menu
  and the prompt chrome on the active palette and save it as that palette's
  default; `/syntax` alone reports the current
  style. See [Code highlighting styles](#code-highlighting-styles) for the list;
  `/theme-preview` renders every style, marking the one in use.
- `/colors terminal`: opt into terminal-defined ANSI colors with unpainted code
  backgrounds and `ansi_dark` / `ansi_light` syntax. `/colors palette` restores
  the default coordinated palette; `/colors` shows the current selection.
  This affects Rich output, not the input/completion palette. You can also start
  with `--color-style terminal` (default: `--color-style palette`). Run
  `/theme-preview` after switching to compare headings, links, quotes, tables, Python, and diffs.
  Existing scrollback is not repainted.
- Session, conversation-tree, model, and tool popups share terminal-default
  backgrounds and text, with reverse-video selection highlights. They follow your
  terminal background automatically, independently of `/theme` and `/colors`.
- `/help` (or `/commands`): grouped command list and keyboard shortcuts.
- `/login`: sign in to Anthropic in a browser; `/logout` removes pcode's stored login. Both require an idle conversation.
- `/model`: searchable model picker for configured providers (keeps the conversation;
  applies from the next request when chosen mid-run).
- `/tools`: scrollable tool-call inspector for the current conversation, including resumed calls.
- `/tools failed`: open the same inspector filtered to failures.
- `/diffs`: browse this conversation's file diffs in a full-screen popup.
- `/links`: pick any URL mentioned in this conversation (your prompts or the
  assistant's replies, newest first) and open it in the default browser via
  `open` (macOS), `xdg-open` (Linux), or the shell association (Windows). Useful
  when the terminal or an older tmux does not make rendered links clickable.
- `/status`: current model, workspace, session storage path, completed turns, token usage,
  and a breakdown of the prompt overhead the model is re-sent every request — see
  [Where the fixed prompt goes](#where-the-fixed-prompt-goes). Opens a popup in the
  interactive editor; prints inline when there is no editor.
- `/resend`: retry from the last checkpoint without a new message; shows the previous prompt and spinner.
- `/compact [focus]`: summarize older context with the current model; keep recent history.
- `/autocompact on|off`: opt into automatic LLM compaction (saved user preference; default off).
- `/new`: start a new saved conversation; clears the screen and retained scrollback, keeps input history.
- `/resume`: browse and search saved conversations by their prompts; resume one in place.
- `/tree`: [browse and fork the conversation](docs/conversation-tree.md); select a user prompt to
  edit it, or an assistant response to continue from there. Existing branches are kept.
- `/skill:NAME [text]`: run a discovered skill; see
  [Skills as slash commands](#skills-as-slash-commands) for naming and configuration.
- `/quit` (alias `/exit`): exit.

### Keys and layout

| Key | Action |
| --- | --- |
| Enter | Send using the active send mode, or accept a selected completion |
| Ctrl+S | Cycle steering → queue → interrupt (saves the default) |
| ↓ | Newline when on the last line with nothing to complete or recall (works in vi insert mode) |
| Ctrl+J / Shift+Enter | Newline; see [Newlines in tmux](#newlines-in-tmux) if neither reaches pcode |
| Alt+Enter | Newline in Emacs mode only (Esc followed by Enter also works) |
| Tab / arrows | Browse completion; arrows also navigate input/history |
| Ctrl+L | Choose a model (keeps the conversation; applies from the next request) |
| Ctrl+O | Show/hide the Tasks/Tools widget (saves the default) |
| Ctrl+R | Search this process's input history |
| Ctrl+G | Mirror commands and their output to scrollback (saves the default) |
| Ctrl+C | Discard input; cancels the running turn only when the prompt is empty |
| Ctrl+D | Exit on empty idle input; cancel during generation |

Press **Ctrl+O** or use `/show-tasks [on|off]` to hide or show the Tasks/Tools
widget without stopping work or clearing task/tool history. The current prompt
and queue remain visible. Visibility is saved across launches (default: on);
use `pcode config set show_tasks off` to set the default from the shell.

The widget also hides itself as soon as the model finishes a turn, keeping the
idle prompt compact, and returns on the next turn. Turn that off with
`/autohide-tasks off` (or `pcode config set autohide_tasks off`); Ctrl+O brings
the widget back immediately after an auto-hide.
Ctrl+O replaces the editor’s insert-newline binding; Ctrl+J still inserts a newline.

**Setting acknowledgements are transient.** Toggles and display settings
(`/show-thinking`, `/show-tasks`, `/show-edits`, `/show-commands`,
`/autohide-tasks`, `/autocompact`, `/theme`, `/colors`, `/syntax`, `/effort`)
answer on a line directly above the spinner, just over the editor, and clear
themselves after five seconds. They never enter terminal scrollback, so
flipping a display option repeatedly does not litter the transcript, and a
transcript rebuild (`/redraw`, resize replay) neither preserves nor duplicates
them. Long acknowledgements wrap to the pane and are capped at six rows.
Everything else a command reports — `/status`, `/mcp`, `/help`, login flows,
session and model changes, warnings, and errors — still goes to scrollback.
Without a live panel (redirected output, `--print`) an acknowledgement falls
back to a printed notice.

### Optional vi editing

The prompt uses Emacs-style editing by default. Enable vi bindings for subsequent
launches with:

```sh
pcode config set editing_mode vi
```

You can also run `/config set editing_mode vi` in a session, then restart pcode.
The editor starts in insert mode; press Escape for normal mode and `i` or `a` to
resume inserting. Use `o` / `O` in normal mode to open a line below / above
and enter insert mode. Standard vi motions and editing commands are available.
Enter still submits (or accepts a selected completion), and Ctrl+J inserts a
newline. Escape is prioritized in vi mode: Escape followed by Enter submits,
rather than inserting a newline. Alt+Enter is therefore a newline shortcut only
in Emacs mode. Other pcode application shortcuts retain their existing behavior.

**Shift+Enter inserts a newline** when your terminal sends a distinct CSI-u or
xterm modifyOtherKeys sequence. Ctrl+J supports both the traditional LF byte and
those extended encodings. If your terminal sends ordinary Enter for Shift+Enter,
pcode cannot distinguish them: configure Shift+Enter to send Ctrl+J (the single
LF byte, hex `0a`, often written `\x0a`). This is a terminal key mapping, not a
pcode setting; verify it inside tmux too if you use it. The ↓ key inserts a
newline whenever it would otherwise do nothing (last line, no completion menu,
not browsing older history), so it works even where no chord gets through.

#### Newlines in tmux

Inside tmux, Shift+Enter arriving as a plain Enter is almost always tmux, not
the terminal. Two things have to be true, and `extended-keys on` alone gives you
neither:

- tmux only asks the outer terminal for modified keys when its terminfo
  advertises `extkeys`; Ghostty's and kitty's do not, so declare it.
- `extended-keys on` forwards those keys only to apps that opted into the
  protocol themselves. pcode (prompt_toolkit) does not, so use `always`.

```tmux
set -as terminal-features ',xterm-ghostty:extkeys'
set -g extended-keys always
set -g extended-keys-format csi-u
```

Reload, then detach and reattach: `#{client_termfeatures}` is computed when a
client connects. Check with `cat -v`: Shift+Enter should print `^[[13;2u`. If
Ctrl+J prints `^[[B` instead, a remapper (Karabiner, a Ghostty `keybind`) is
turning it into ↓ before tmux sees it; ↓ still inserts a newline on the last
line, so that is usually fine.

Vi mode uses a 100 ms terminal escape-sequence timeout and an eager Escape binding.
This avoids waiting for an Alt-key chord before entering normal mode; particularly
slow or fragmented terminal connections may need a longer timeout in future.

Restore the default with `pcode config set editing_mode emacs` or
`pcode config unset editing_mode`.

The input is bottom-aligned from startup, with one editable line plus its border.
It expands upward for wrapped text or explicit newlines, and shrinks when text is
removed. Completion appears above the frame. Very long input scrolls within the
available pane height. Multiline bracketed paste works; mouse capture is off.

Wrapping is word aware: a word that would straddle the right edge moves to the
next row whole, instead of being cut in half. The buffer text is unchanged — the
padding is display only, so editing positions, selection, and what gets sent are
all unaffected. A single word wider than the pane still has to be split.

Tasks and recent tool activity share one compact, headerless widget above the
editor. Task rows show status icons and keep the active item visible. Up to five
recent tool calls appear as indented subitems immediately below the active task;
this rolling view follows the currently active item, rather than recording
historical task ownership. When there is no active task (including no plan),
tools appear as unparented rows in the same widget instead of beneath a completed
or pending task. There is no separate Tools panel or Tasks heading.

The shared height budget shrinks in small panes, preserving the active task and
the newest tool calls. Empty tool slots are not reserved, and an empty widget is
hidden. Each call updates in place from running to success/failure; cancellation
marks unfinished calls as interrupted, not undone. Rows show a status icon, tool
name, elapsed time, and truncated path/command summary, without call numbers or
run counters. The latest ten calls remain in bounded internal history, and saved
session resume restores this history independently of conversation replay.
Task additions and status changes preview as soon as their fields arrive in the
model's streamed tool arguments, without waiting for the full response or tool
execution. These previews are display-only: tool results reconcile them to the
confirmed plan, and cancellation or failure discards any unconfirmed preview.
Only confirmed plans are saved. Successful planning operations update the task
rows without duplicate tool rows; failed planning operations remain visible as
failed calls. Plans and tools persist across turns and saved-session resumes;
`/new` clears both.
Routine tool summaries no longer enter conversation scrollback. `/tools` opens a
read-only alternate-screen inspector, separate from this ten-call activity panel.
It retains all live conversation calls, including successful planning operations.
The non-interactive `--theme-preview` sample still prints its fictional tool
summaries.

### Edit diff browser

`/diffs` opens a full-screen popup showing this conversation's completed file
edits, using the same diff colors as scrollback. The diff fills most of the
screen; a small file selector sits at the bottom. Keys are listed in the header:

- Up/Down in the file list selects a file, newest change first.
- PageUp/PageDown scroll the diff without leaving the file list.
- Tab/Shift+Tab move focus; arrows and Ctrl+Home/Ctrl+End scroll the focused diff.
- Escape, Ctrl+C, or Ctrl+D closes the popup and restores the editor draft.

Saved sessions read their changes back from the journal on the active branch, so
resumed and branched conversations show the diffs that belong to them. Redaction
and size limits are the same as the scrollback blocks; nothing is re-read from
disk and no edit is re-applied.

### Tool-call inspector

Use `/tools` or `/tools failed`, including during an active turn.
The inspector shows a snapshot of the calls available when opened; reopen it to
see newer results. The model keeps running while the inspector is open, and
terminal output is buffered until it closes. Inspection never reruns a tool.

- Calls are newest first. Use arrows to select and Tab/Shift+Tab to move between
  the call list, detail pane, and search field.
- In the call list, **f** toggles failures, **t** cycles tool-name filters, and
  **/** focuses search. Search matches tool names/statuses and command/summary
  previews, not the complete output payload. Ctrl+F focuses search from any pane.
- In details, use arrows to scroll by line, PageUp/PageDown by page, or Ctrl+U/Ctrl+D
  by half a page. The session browser shares these content-pane controls.
- Mouse clicks and wheel scrolling work in the popups. In tmux, enable mouse
  forwarding with `tmux set -g mouse on` (or `set -g mouse on` in `~/.tmux.conf`).
- Escape or Ctrl+C closes only the inspector and restores the editor draft.
  Ctrl+D also closes it when the call list or search field has focus.
- Wide terminals show calls and details side by side; narrow terminals stack them.

Details include the call/run IDs, timestamp and duration when captured, structured
arguments, framework outcome, and returned output/error. Nonzero command exits,
timeouts, and tool retries are failures; interruption and unknown results remain
distinct. Background launch/check/stop calls show their process ID and related
calls when available. A successful launch is not proof that the process finished
successfully.

Saved inspection data is a redacted display projection in `transcript.jsonl`, not
an execution or recovery log. It survives resume and failures in later model
requests. Metadata is indexed incrementally; result payloads are read on selection.
Arguments and results are each capped at 128 Ki characters, with explicit truncation
markers. Tool-side truncation is preserved, not recoverable by the inspector.
Unsaved conversations retain at most 8 MiB of payload text; older evicted details
are labeled while call metadata remains. `/new` resets the inspector history.
Older saved calls remain browsable but show a missing-details label where their
journal did not capture arguments/results. Preview mode shows its recent fixtures.
Redaction is best-effort, not a guarantee that arbitrary sensitive content is removed.

The editor avoids full-screen erase sequences that terminals such as tmux can
copy into scrollback, leaving duplicate borders after a resize. Repeated
horizontal and vertical shrink/grow cycles, including multiline drafts, are
covered by real-tmux tests with cursor-position reports enabled.

Live responses stream as literal text into normal terminal scrollback, including
Markdown markers. Complete lines are printed once; only the unfinished display
line is live. Rich's `Text.wrap()` wraps at spaces using the current terminal width, keeping
words together across streamed chunks. The separating space becomes a newline;
explicit newlines and indentation are retained. Tokens longer than the available
width must still split. The live tail stays small. Finishing a message flushes the
tail without replacing the response
with rendered Markdown. Tool summaries live inside the task widget.
`/theme-preview` and restored session messages still use Rich Markdown.

The editor remains usable throughout generation, including multiline input,
history, slash completion, and `@` file references. Enter sends using the active mode (steering by
default) and clears the editor for another draft; the toolbar shows the mode and
pending message count. Steering messages join the next model request; queue-mode
messages run in order after the current turn finishes. Ctrl+S cycles send modes. Slash commands use a separate async
handler, so help, inspection, theme, context, and effort controls remain available
while the model works. `/model` also opens while working and applies from the next
request. `/new`, `/resume`, `/login`, and `/logout` require an idle conversation: cancel
or wait, then retry. `/quit` (or `/exit`) cancels the active run and waits for its
cleanup before exiting. Ctrl+D cancels the current turn, clears queued messages,
and preserves the unsubmitted draft and cursor. Ctrl+C discards the draft first,
so cancelling with it takes a second press when the prompt has text. A failed turn also
clears queued messages rather than automatically running more requests. Commands
are not discarded with that queue. The queue is in memory only, not saved until
submitted to the runtime.

Use terminal/tmux scrollback, selection, and search for conversation history.
The conversation has no alternate screen or application-owned viewport; only the
temporary tool inspector uses the alternate screen. Resizing
does not re-render completed responses; reflow is up to the terminal, and explicit
line breaks remain. Known limitation: narrowing a pane during streaming can leave
a copy of the unfinished line in scrollback when prompt_toolkit erases its old
layout after the terminal has reflowed the input frame. A tmux regression test
tracks this as an expected failure; committed lines are not reprinted.

Editor history remains in memory; live model messages and transcript events are
saved unless `--no-save` is set. Failed/cancelled runs recover settled checkpoints
when safe. Cancellation never undoes completed tool effects.

## Small architecture

For a new interactive session, the editor opens before the live backend is ready.
Agent imports and construction run in a worker thread; the toolbar shows `starting`
and you can type immediately. Submitted prompts and agent-dependent commands wait
for initialization; Ctrl+C clears queued work. Optional model metadata refresh runs
in the background, outside the first-paint path. Resume still opens and validates
the saved session before the editor starts, then gates requests on recovery.
Shutdown waits for in-flight construction to finish so late-created runtimes are
cleaned up rather than abandoned.

- `src/pcode/agent.py`: `Agent(model, capabilities=[Coder(workspace)])` definition;
  independent of the terminal.
- `src/pcode/live.py`: `run_stream_events()` adapter, history, and usage. It runs the
  whole tool loop, including when the model emits text before tool calls.
- `src/pcode/runtime.py`: plain application events and offline fixtures.
- `src/pcode/sessions.py`: private manifests/journals, session locking, and the
  official Harness SQLite step store; recovery uses its settled snapshots.
- `src/pcode/diagnostics.py`: structured provider errors with best-effort redaction.
- `src/pcode/ui.py`: prompt_toolkit editor and bottom-aligned layout, plus a batched
  terminal writer for committed Markdown blocks.
- `src/pcode/commands.py`: registry shared by dispatch, help, and completion.
- `src/pcode/file_refs.py`: cached workspace file listing behind `@` completion.
- `src/pcode/inspection.py`: bounded inspection projection and lazy journal index.
- `src/pcode/inspector_ui.py`: alternate-screen tool selection, filters, and scrollable details.
- `src/pcode/app.py`: CLI and asynchronous composition.

prompt_toolkit owns the activity panels, menus, and editor in the normal screen.
`TerminalOutput` batches writes through `in_terminal()` at up to 30 updates per
second, briefly repainting the prompt without resetting its buffer or cursor.
It never holds a terminal handoff across a model/network wait. Rich renders completed
Markdown blocks (including highlighted code and tables) once into scrollback. The
unfinished block stays hidden until ready; lists, quotes, and open code blocks
may remain buffered until a following block or the end of the response. The
running-prompt spinner and task/tool activity remain visible while text is buffered. Cancellation and tool boundaries flush any remaining text. Already
committed blocks are not rewritten, so reference links defined in later blocks
cannot retroactively update earlier output. `--theme-preview`
remains a noninteractive print-and-exit command.

**Transcript means persistent scrollback.** Anything written to `Transcript` should
remain in normal terminal/tmux history. Assistant prose uses Rich Markdown;
submitted prompts and compact error/warning notices use literal Rich renderables,
not Markdown or panels. All persistent writes share the existing batched terminal
handoff. Ordinary informational notes remain subdued.

`PreviewApp.present_events()` routes tool starts and completions into mutable tool
history. At completion, the adapter's semantic `ToolSummary.failed` flag also
selects exceptional outcomes for persistent diagnostics (including non-zero shell
exits and failed tests/builds/lint/typechecks). Successful routine tools remain
live-only; explicit demo/inspection output is separate. Failure excerpts are
bounded and sanitized by the existing tool adapter. An exceptional completion
flushes pending assistant prose before its diagnostic, preserving event order.

Core events have no Rich or prompt_toolkit dependencies. Provider-exposed readable
thinking streams as `ThinkingDelta` and completes with `Thinking`; both are saved
independently of display visibility. `Transcript.thinking` renders it in muted,
dim scrollback when enabled and retains it for redraw when hidden. Opaque signatures
and provider-internal reasoning are not readable transcript content. Plans, status,
dialogs, and editor state remain mutable, outside `Transcript`.

Approvals, model pickers, and MCP management are not implemented yet. Model
request-count limits are explicitly disabled; there is no monetary budget guard.

## References

- [prompt_toolkit prompts](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html)
- [Rich Console](https://rich.readthedocs.io/en/stable/console.html)
- [Pydantic streaming events](https://ai.pydantic.dev/agents/#streaming-all-events)
- [Pydantic Harness Coder](https://ai.pydantic.dev/harness/coder/)

The latest Harness website describes a newer Coder composition than the pinned
0.31.x release. Implementation follows the installed release's public API.

## Status line

The line below the editor shows the workspace/branch, full `provider:model`
identifier, reasoning effort, and activity. Live models also show context, for
example `ctx: 12.5k/200k` (tokens used / effective working window).

Used context is the **latest completed request's input tokens**, including cached
input, not cumulative session usage. It updates after a turn and follows the
selected conversation history when resuming or navigating branches. It does not
include unsent drafts, subsequent tool results, or a response still streaming.
The working window is shared with compaction. Pcode first uses metadata from the
actual serving provider: Codex's authenticated models endpoint or Anthropic's
Models API. Otherwise it uses an exact provider/model match from
[Models.dev](https://models.dev/), where the serving endpoint matches. Codex never
borrows ordinary OpenAI API limits, and custom proxies or Anthropic subscription
OAuth do not silently inherit direct-API limits. Catalog values remain advisory.
When separate input and combined-context ceilings are available, the smaller
ceiling is used; output capacity is retained separately, not added to input.

Metadata refreshes happen outside rendering, on startup, model selection, and
before requests when stale. Public metadata is cached for 24 hours in
`$XDG_CACHE_HOME/pcode/model-context-v1.json` (default `~/.cache/pcode/`). Native
metadata is cached for one hour **in memory per model instance**, not across
accounts or processes. Fetches have a three-second deadline and failures retain
last-known values with a one-minute retry backoff. No pricing is loaded into the
context cache or displayed. Unknown limits show `?`; a first run offline can thus
show `?`, and Codex needs a successful native lookup or an explicit override.
Used context shows `0` when the conversation is empty or usage has not been
reported yet.
Long paths shrink first; narrow terminals may truncate trailing context details.
`/status` continues to show cumulative session input/output usage.

## Where the fixed prompt goes

`ctx:` is one number, which does not say why it is that large. `/status` also
breaks down the **prompt overhead**: the instructions and tool
schemas the provider is re-sent on every request, whatever the conversation did.

```
Prompt overhead           ~7.4k tokens · 3% of 200k · estimated
  Instructions            ~4.3k
    ~/AGENTS.md           ~2.8k · instructions
    AGENTS.md             ~675 · instructions
    Harness base prompts  ~228
    Planning tool         ~162
    Assistant config      ~105 · paths only · 1 skill
    File tools            ~102
    Sub-agents            ~90
    Web research          ~74
    Tool output limits    ~60
    Terminal instructions ~15
  Tool schemas            ~3.2k · 16 tools
    Largest               write_plan 625 · grep 314 · edit_file 310 · shell 259
```

Each repository instruction file gets its own row, so it is obvious when a global
`AGENTS.md` costs more than everything else combined. `Assistant config` is the
discovery block: **skills cost a path, not a body**, because pcode passes their
location and the model reads `SKILL.md` with a tool only when the skill runs. See
[Skills as slash commands](#skills-as-slash-commands).

The rows are read from the last request's resolved instructions and tool
definitions, not re-derived, so they describe what was actually sent. Before the
first request there is nothing to attribute and the row says so. Token counts are
the same 4-characters-per-token estimate compaction uses, so they are comparable
with its threshold rather than exact provider counts.

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

## MCP servers (explicit opt-in)

MCP is **off by default**, with no automatic discovery. Configuring a server does
not start it or add its tool definitions to model requests. Use:

```text
/mcp list
/mcp enable fetch
/mcp disable fetch
```

`/mcp` also lists servers and the configuration path. Tab completion includes
configured server names for `enable` and active names for `disable`. These are
local commands; they do not make a model request. Enable servers individually.

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
  inferred from the URL by the MCP client. Add `"auth": "oauth"` for browser sign-in.

Either transport also accepts `"direct": true`; see tool search below.

Server names start with a letter and contain letters, digits, `_`, or `-` (up to
32 characters). Unsupported server fields are rejected on enable rather than
silently ignored. String values support `${VARIABLE}` and `${VARIABLE:-default}`.
Only the selected server's variables are expanded, at enable time, so missing
credentials for an unused server do not block ordinary work. Keep secrets in the
environment rather than the JSON file.

### OAuth sign-in

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
  and the MCP SDK. Servers must support that client flow; pre-registered client IDs,
  custom scopes, and fixed callback ports are not exposed in pcode's config yet.
- **Credentials are in memory only.** They are reused across turns while the server
  stays enabled. Disable/re-enable, `/new`, session resume, or process restart
  creates a fresh OAuth client and may require browser sign-in again. Closing a
  turn's connection does not discard the enabled client's tokens. No OAuth tokens
  are written to pcode's configuration, session files, or a persistent token store.
- Do not combine OAuth with an `Authorization` header. Non-auth headers may be used
  alongside OAuth. For a static bearer token, continue using `headers` with an
  environment variable reference instead of `auth`.
- Disabling a server drops pcode's reference to its OAuth client; it does not revoke
  the server-side grant. Revoke access through the service if needed.

### Tool search (`direct`)

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

### Activation and token usage

- `/mcp enable NAME` makes that server's tools available on subsequent turns in
  the **current conversation**, including all tool/model steps within a turn.
  Switching models keeps the selection. Repeating `enable` is a no-op.
- OAuth servers connect during `/mcp enable`, then disconnect while retaining
  their in-memory tokens. Non-OAuth servers still connect only on the next turn.
  All enabled servers reconnect for each turn and close afterward, including on
  failure or cancellation; local subprocesses do not stay running between turns.
- `/mcp disable NAME` removes those tools from subsequent model requests. MCP
  selection cannot change during an active turn. To reload a server after editing
  its configuration or environment, disable and enable it again.
- New conversations (`/new`), resumed conversations, and application restarts
  start with **all servers off**. Activation is never saved in session files or
  user defaults. `/mcp list` shows the current state.
- Off servers contribute **no MCP tool schemas or server instructions**. Enabled
  tools are namespaced as `mcp_NAME_TOOL`; their results consume context normally,
  and their schemas do too once they are direct or discovered. Disabling does not
  erase earlier tool results from history.
- Enabling authorizes the agent to use the server's tools with that server's
  permissions, including write actions. There is no additional per-call approval
  or sandbox. Server instructions are not automatically added to the prompt.

## Validate

```sh
make test        # fast suite in parallel; real-tmux regressions skipped
make test-all    # everything, including the real-tmux regressions
uv run ruff check .
uv run ruff format --check .
```

The real-tmux tests are 64 of ~1580 tests but three quarters of the suite's
runtime, so they are opt-in: `make test` skips them (each skip states why) and
finishes in seconds, while `make test-all` runs them. Ad-hoc `pytest` invocations
follow the same rule: `--tmux` or `PCODE_TEST_TMUX=1` enables them, and naming a
tmux path (`uv run pytest tests/test_tmux.py -k resize`) counts as asking for
them. They run serially on purpose: they assert on real pane paints within
deadlines, and a loaded machine makes them fail spuriously. Run `make test-all`
before pushing anything that touches layout, streaming, or the editor.

Tests require no API keys or paid model calls. They cover completion, keybindings,
Unicode/narrow output, streaming, history/reset, cancellation, and actual Coder
file reads using Pydantic's `FunctionModel`. Session tests cover round-trip history,
post-tool failures, safe diagnostics, file permissions, locking, torn journals,
and resuming interrupted tools without replaying them or clearing their effect
ledger. A native-provider wire test checks that explicit cache markers are omitted
while streaming/store settings are retained.
PTY tests check clean startup/exit without an alternate screen. Queue tests verify
serial turns, failures, cancellation, and draft/cursor preservation. When tmux is
installed, isolated-server tests measure prompt height and bottom placement
through splits, streaming, cancellation, and replies. They also verify long
responses in scrollback, no completion-time replacement, and draft editing during
resize. One expected failure tracks the unfinished-line width-resize limitation.

Real tmux tests include cursor-position reports: plain PTYs alone missed the
original frame-stretching bug. Actual copy-mode/search and rendering in your
terminal still deserve a manual feel check.

### Command previews

Shell tool calls show a compact two-row preview with their result and duration.
Long arguments and embedded scripts are abbreviated; short commands remain readable.
Previews are redacted and terminal-control sanitized. Failure excerpts remain visible.
There is currently no command to expand previews or show full command outputs.


### Model output limits

Anthropic requires a `max_tokens` ceiling for each response, including thinking
and tool-call arguments. Pcode resolves that ceiling from the serving model's
metadata on each request (also for delegated agents and after model switches).
Authenticated metadata takes precedence; public catalog limits are used only for
matching provider endpoints. If no output limit is known, pcode uses 16,384 tokens
instead of Pydantic AI's 4,096-token fallback. Explicit model settings take precedence,
and other providers keep their existing defaults.

This is a ceiling, not a requested response length or reasoning budget. Thinking
visibility and effort settings do not change it. The compaction summarizer retains
its separate, smaller output budget. Automatic compaction accounts for the resolved
ceiling, but reserves at most half the working window so small context overrides
remain usable. Provider limits still apply; truncation is not automatically retried.

### Tool output limits

Pcode uses [Harness ToolOutputLimits](https://pydantic.dev/docs/ai/harness/tool-output-limits/)
to reduce large results **once, before they enter model history**. By default, a
result of 10,000 characters or more is stored on disk; the model receives a handle
and a 1,000-character head/tail preview, plus a small retrieval header. Smaller
results pass through unchanged. This replaces Coder's 64,000-character truncation
and applies to the main agent and explorer, including web/MCP tools and delegation
results. It makes no extra LLM calls and does not require automatic compaction.

```sh
pcode config set tool_output_mode spill          # Default: store, preview, read back
pcode config set tool_output_threshold 8000      # Trigger at 8,000 characters
pcode config set tool_output_preview_chars 800   # Content preview, excluding headers
pcode config set tool_output_max_chars 3000      # Fallback if storing fails
pcode config set tool_output_strategy head_tail  # Truncation keeps both ends
pcode config set tool_output_retention_hours 168 # Optional: prune spills older than a week

pcode config set tool_output_mode truncate       # Lossy, no new spill files
pcode config set tool_output_mode off            # No new result reduction
pcode config unset tool_output_mode              # Restore default spill mode
```

These settings also work through `/config`, with tab completion and validation.
They are snapshotted when the agent is constructed; restart pcode to apply them to
an existing conversation. They do not rewrite oversized results already in history.
All budgets are characters, not tokens. Keep the preview and truncation budgets
below the trigger threshold to save context. Spill previews always show both ends;
`tool_output_strategy` applies only to truncation and the spill-failure fallback.

The model uses `read_tool_result(handle, offset, limit, from_end, pattern)` to
retrieve selected lines or literal substring matches. Readback is exempt from
reduction and bounded by Harness to 1,000 lines / 50,000 content characters per call.
Structured returns are stored as indented JSON for paging. For a single line longer
than the readback cap, the agent is also told how to read a character range from the
spill file with shell. Retrieval stays available in `off` and `truncate` modes so
older handles still work after resuming a saved session.

Spills live in `$XDG_STATE_HOME/pcode/tool-results` (default
`~/.local/state/pcode/tool-results`), under an owner-only directory shared by pcode
workspaces and runs. They contain **raw tool output**, not the terminal's redacted
projection, and are written even with `--no-save`. This is local storage, not an
isolation boundary or encrypted credential store. To avoid new spill files, use
`truncate` or `off`; that does not delete existing spills, sessions, or shell logs.
By default spills are kept indefinitely. A nonzero retention schedules best-effort
background pruning on new writes, based on modification time, not last access.
Pruning or deleting files can break old handles; the read tool then asks the model
to rerun the original tool. Reset retention to `0` to disable future pruning.

Spilling preserves the result received by the limiter, not data a tool already
omitted. File-read pagination and the shell's native 16 KB output-tail cap still
apply, even in `off` mode. The full command output remains in the shell log. Shell
PID/log/status handles are kept outside the reduction budget, including with tiny
budgets or head truncation. Reduced shell bodies are omitted from the inspection
projection when their clipped text no longer has reliable redaction context; the
live preview remains separate. Store failures fall back to lossy truncation.
LLM summarization, multiple size bands, and per-tool configuration are not exposed.

### Context compaction

`/compact` makes a tool-free LLM call using the current model/provider credentials.
Optional instructions add focus without replacing the standard continuation summary:

```text
/compact
/compact Preserve auth debugging findings, exact file paths, and failing tests
/autocompact on
/autocompact off
```

The summary preserves goals and constraints, decisions, current state, exact artifacts,
verification results, and next steps/blockers. Recent messages are retained verbatim
with a token budget (up to 20k, scaled down for smaller windows); a single oversized
settled tool batch is summarized too rather than splitting its call/result pair.
Repeated compaction updates the previous summary. Summarizer tool-result input is
capped at 16k characters per result rather than Harness's default 500 characters.
Summaries are lossy: pre-compaction tool results remain available through the
session/tool history (including spill handles for reduced results), and the model
should retrieve spilled output or re-read source files when exact details matter.

Manual compaction requires an idle live session. Ctrl+C cancels it; queued prompts
wait until it finishes and are cleared on cancellation/failure. Short histories are
a no-op. Empty, invalid, or non-shrinking summaries are rejected without changing
active history. The result shows estimated before/after tokens; the context indicator
uses `~` until a new provider response supplies a measured count. Summary requests
contribute to session usage totals, not the completed-user-turn count.

Each successful manual compaction adds a selectable `/tree` checkpoint on the current
branch. It survives restart immediately, even without a subsequent prompt. Original
checkpoints, sibling branches, plan IDs/state, transcript, and tool-effect records are
retained. Navigating to a compaction checkpoint never runs a model or replays tools.
Unsaved sessions keep the same checkpoint in memory. Compaction is not deletion or
redaction of the saved conversation.

Automatic compaction is **off by default**. `/autocompact on` saves a user preference
in `~/.config/pcode/preferences.json` (or `$XDG_CONFIG_HOME/pcode/preferences.json`).
When enabled, pcode checks before every model request, including inside tool loops,
using provider usage plus estimated new input/tool results and tool schemas. It
triggers around 80% of the deployment window, with additional output headroom.
Automatic summaries are persisted as safe checkpoints of the current run before the
next request. If compaction cannot make enough room, the run stops with an error;
it does not loop over summaries, silently drop history, or replay completed tools.
There is no automatic retry of provider context-overflow errors in this version.

Model catalog windows are advisory. Unknown deployments skip automatic compaction;
enabling it interactively requires a known window or an explicit override. For a
custom proxy, gated model window, or incorrect catalog entry, set the actual limit:

```sh
PCODE_CONTEXT_WINDOW=128000 pcode --model your-provider:your-model
```

The override applies to both the status line and compaction in the current process,
including model switches; update it if you change deployments. It is capped by
known provider input/maximum-context limits. For Codex, a larger window is only
used explicitly when its metadata advertises that maximum; pcode does not opt into
long context just because a generic model catalog advertises it. A summary can still fail if the
existing history itself is too large for the summarizer request. Failure leaves the
source history available rather than falling back to destructive truncation.

Pcode removes Coder's default clearing of old tool results at 70% context usage so
that evidence is not discarded before the summarizer sees it. With auto-compaction
off, use `/compact` proactively or `/new` for unrelated work.

### Saved thinking in scrollback

Press **Ctrl+T** or use `/show-thinking [on|off]` to show or hide provider-exposed
thinking text. When enabled, thinking streams into normal terminal scrollback in
a muted, dim style, distinct from the answer. Complete lines are printed as they
arrive; the unfinished last line is flushed at the block boundary or when a turn
ends, fails, or is cancelled. Thinking no longer appears in the Tasks/Tools
header or a separate live panel.

The toggle saves the default and triggers the same retained-transcript rebuild
as `/redraw`, so it reveals or hides earlier thinking too, including text received
while hidden. It works during a turn and after completion, preserves the editor
draft, and does not duplicate answer/tool output. The usual redraw limitations
apply: regeneration clears pre-pcode terminal history and projects the bounded
retained transcript, not an unlimited terminal archive. Redirected output cannot
be retroactively erased or redrawn.

```sh
pcode config set show_thinking on   # Default is off
```

**Privacy and persistence:** readable thinking is recorded in saved sessions even
when hidden. Resuming a session restores thinking alongside its recent transcript;
the complete text remains in the session journal. Interrupted blocks are retained
too. Display redaction is not redaction of session files. Provider signatures and
redacted/opaque thinking blocks are not printed or added to the readable-thinking
journal events, but native model-message history may still contain them for
continuation. `--no-save` disables session persistence, not in-process replay or
terminal scrollback. Turning visibility off is not secure deletion of terminal,
log, or session history. Older sessions without thinking journal events cannot
retroactively populate this view from their native model history.

There is no longer an 8,192-character thinking tail or a thinking-row limit.
Legacy `thinking_display` and `thinking_lines` preferences are ignored. Provider
summaries may themselves be abbreviated: this view shows the readable text the
provider actually exposes, **not hidden internal reasoning**. Delegated agents'
thinking is still not forwarded into the parent's transcript.

For direct Anthropic models, enabling the view also requests visible thinking on
the next turn: adaptive thinking for supported models, otherwise an explicit
2,048-token legacy budget, with `display: "summarized"`. This can increase latency
and token usage. Turning it off removes that request override and restores provider
defaults, rather than explicitly disabling reasoning. In-flight requests and the
separately selected effort are unchanged. Codex requests `summary: "auto"`
independently of visibility. Meridian still needs upstream thinking generation and
thinking forwarding; pcode does not mutate an external proxy's global settings.
See the Meridian setup section for the isolated managed-instance option.

### Error logs in scrollback

Errors and failed-tool diagnostics render as fenced Markdown code blocks using
Rich and the active code theme. Logs stay literal, even if they contain Markdown
or backticks. By default, each error shows at most **20 wrapped body lines**, plus
its heading and two code-block padding rows. Long logs keep their tail and include
a truncation marker within that limit.

```sh
pcode config set error_scrollback_lines 40  # Positive integer; default 20
```

This line limit also works through `/config` and applies on the next launch.

By default, a failed tool call does not write its diagnostic to scrollback. It
keeps the same compact summary line a successful call leaves, marked `✗` in the
error colour instead of `✓`, so the failed call stays visible without its log.
Enable `tool_error_scrollback` for the full diagnostic:

```sh
pcode config set tool_error_scrollback on  # Failed-tool diagnostics (default off)
```

Command failures still follow command visibility below: with mirroring off they
stay out of scrollback entirely, and with mirroring on they show their summary
line, adding the captured output only once this option is on. Application
errors remain visible either way, as do warnings and cancellation notices.
Saved diagnostics are not disabled or trimmed by these display settings.
Command diagnostics retain a separate safety bound of 200 lines / 32,000
characters, after redaction.

### Command output in scrollback

By default, commands stay in the mutable tool panel, including failures. Enable `show_commands` to mirror **every settled
shell tool call and its captured output** into permanent terminal scrollback
(a failed call mirrors its output only with `tool_error_scrollback` on):

```sh
pcode config set show_commands on             # Mirror commands and output (default off)
pcode config set command_scrollback_lines 80  # Positive integer; default 20
pcode config set command_preview_lines 10     # Live output height cap; default 10
pcode config set show_commands off            # Hide all commands, including failures
```

Each mirrored block shows a success/failure indicator, the tool label, elapsed
time, and a shell-highlighted invocation on a `$` line. Captured output stays
literal, with its indentation preserved and no Markdown parsing or extra block
padding. Process polling details without a command are shown without a `$` prefix:

```text
✓ Run · 0.4s
  $ pytest -q
  2 passed in 0.31s
  {"pid": 124, "exit_code": 0}
```

Details:

- It covers the current `shell` tool, including delegated calls. Saved legacy
  `run_command`, `start_command`, `check_command`, and `stop_command` entries also
  remain displayable. Other tools are unaffected.
- Active foreground `shell` calls show a preview above the prompt, refreshed as
  complete lines arrive from the combined stdout/stderr log. Harness emits at most
  the first 16,000 bytes; a capped preview is marked, and further output stays in
  the command log. Ctrl+G controls both preview and scrollback.
  `command_preview_lines` caps the live output at 10 wrapped rows by default
  (positive integer, excluding the command and frame borders). The preview uses
  space left after the editor, queued prompts, and Tasks/Tools panel. Under tight
  height pressure, task rows yield only enough to retain a one-line output tail.
  For parallel calls,
  the most recently updated command is shown; all calls remain in the tool panel.
  Programs that buffer their own output must flush it (for example, `python -u`).
- On completion the transient preview disappears and one bordered result is
  written to scrollback, without duplicate streamed lines. Preview updates are
  not saved in session history. Background `shell` calls return PID/log/status
  handles rather than streaming output after the call ends.
- Failed commands follow the same show/hide setting as successful commands.
  When shown, they print one block containing captured output (or the saved
  diagnostic if output is unavailable). There is no separate error visibility option.
- Output is redacted and sanitized before display, then bounded to
  `command_scrollback_lines` wrapped output rows (default 20), taken from the end.
  An upstream-truncated tail may start inside a credential with its opening marker
  missing. In that case pcode omits the tail from scrollback and inspection, keeps
  the process/log/status handles, and leaves the raw model result unchanged.
  A separate omission marker counts omitted rendered rows. The command, marker,
  and subtle top/bottom borders are outside this budget, so even a budget of one
  retains the final output row. The capture step retains its own 128 KiB payload bound.
- Verbose commands can push earlier conversation out of terminal history, so
  raise your terminal or tmux scrollback limit before enabling this.

Press **Ctrl+G** to turn mirroring on or off for the rest of the session; it also
saves the default, so the next launch starts in the state you left.
`/show-commands on` and `/show-commands off` do the same, and bare `/show-commands`
toggles. Toggling rebuilds the retained scrollback immediately:
turn it on to reveal earlier captured commands and their outputs; turn it off to
remove all command blocks, including failures. No commands
are rerun. Future completions use the same setting.

Ctrl+G toggles command output outside history search; inside search it retains
its native cancel behavior. Ctrl+S now cycles send modes instead of opening
forward incremental search. Ctrl+R still opens history search. prompt_toolkit
disables terminal XON/XOFF flow control while the prompt is active, so Ctrl+S
reaches the application instead of pausing terminal output.

These settings also work through `/config` and apply on the next launch.

### Edit diffs and streaming previews

Completed `edit_file` and `write_file` calls show compact unified diffs in
scrollback by default. These compare the contents used by the operation, not
Git's working-tree diff, so they don't fold in earlier user changes. New files
are marked as created; unchanged files have no patch. Failed calls aren't
presented as successful edits.

While the model generates a file-tool call, a bounded preview shows the proposed
replacement or write content, labeled **not applied**. It only exposes completed
lines from incomplete arguments. This preview uses the live panel's shared
height budget (`command_preview_lines`), independently of command-output
visibility. It disappears on execution, cancellation, or failure and is never
saved as an applied change. Providers that send arguments all at once may have
no visible streaming phase.

```text
/show-edits off   Hide edit blocks and previews, and redraw retained scrollback
/show-edits on    Show them again, including previously hidden completed diffs
/show-edits       Toggle visibility
```

The choice is saved for the next launch. `pcode config set show_edits on|off`
also sets the startup default. Like `/redraw`, toggling rebuilds terminal history;
it does not rerun tools or change files.

Completed diffs are saved in the session journal and restored with the recent
transcript on resume, even if the files have since changed. Hidden diffs are
still retained; hiding is not deletion. Old sessions without captured diffs
continue to show their existing transcript without reconstructing file changes.

Diff capture omits sensitive paths, redacts recognizable credentials and terminal
controls, and bounds file inputs to 256 Ki characters / 4,000 lines. Saved previews
are capped at 400 patch lines / 64 Ki characters; each displayed block shows at
most 60 wrapped patch rows, with an omission marker. Binary, unreadable, and large
before-snapshots get an explicit unavailable notice instead of a misleading patch.
These previews aren't guaranteed to be applicable patches. Shell commands,
formatters, external writers, and other tools are outside this capture mechanism.

### Regenerating the terminal transcript

`/redraw` rebuilds the retained transcript at the current terminal width and with
current display settings. Ctrl+G, `/show-commands`, `/show-edits`,
`/theme`, `/colors`, and `/syntax`
use the same replay mechanism. The draft, active tool panel, and unfinished model
text are preserved; replay neither calls tools nor changes model history.

The transcript also rebuilds automatically after a terminal size change settles.
Height changes rebuild history too, so live preview fragments do not remain in
scrollback after the pane shrinks. To disable automatic replay:

```sh
pcode config set regenerate_on_resize off  # Default on; applies on next launch
```

Resize replay is debounced to avoid rebuilding on every intermediate size during
a drag. Without it, the editor still resizes normally; `/redraw` remains available
for an explicit transcript reflow.

**Terminal-history warning:** regeneration clears the terminal's visible screen
and scrollback, including shell output from before pcode started. It then rebuilds
only the transcript retained by this pcode process. This uses the normal-screen
ANSI erase-scrollback sequence (verified in tmux); terminals that ignore that
sequence may leave older copies in history. Redirected/non-terminal output is not
cleared or replayed.

The in-memory replay log retains the latest **2,000 presentation entries**,
including hidden command results. An entry can be a Markdown block, a completed
tool result, a notice, or a separator. If older entries have been evicted, replay
shows an omission notice. Saved sessions and diagnostics are unaffected. Session
resume still loads its existing bounded transcript preview; replay does not load
missing command payloads or reconstruct the complete on-disk session archive.

### Sending while the agent is working

Enter uses the saved `send_mode` (default: `steering`). **Ctrl+S** cycles
`steering` → `queue` → `interrupt` and saves the selection; the status bar shows
which mode is active directly under the editor. Mode and working status take
priority over model and path metadata in narrow panes. Existing queued messages
keep their submission mode.

- **steering**: deliver input at the next model request, after active tools finish.
  Pending input is labeled “Steering (next model request)”; once delivered, it
  replaces the active prompt in the task bar. If the turn finishes before then,
  send it as a follow-up turn.
- **queue**: wait for the current turn to finish, then start a follow-up turn.
- **interrupt**: cancel the current turn, discard pending messages, and send the
  new message after cancellation cleanup completes.

Set the default with `pcode config set send_mode steering` (or `queue` / `interrupt`).
`/config set send_mode queue` changes the default for the next launch; Ctrl+S
changes it immediately. Idle input starts a normal turn in every mode. Slash
commands retain their existing behavior, and Ctrl+D (or Ctrl+C on an empty
prompt) still cancels and clears pending messages.

### Running a command yourself: `!command`

A message starting with `!` runs the rest as a shell command in the agent's
working directory and environment, without asking the model anything:

```text
❯ !make test
```

Output streams into the live command panel while it runs and is mirrored to
scrollback when it ends (the last `command_scrollback_lines` rows). Ctrl+C kills
the command and its process group. There is no timeout.

The model hears about it with your next message, as if it had called the
`shell` tool itself: the request carries a `shell` tool call for that command
followed by its output (and `[exit code: N]` when non-zero). The result goes
through the same `tool_output_*` reduction as real tool results, so a long test
log over `tool_output_threshold` characters is spilled to a `read_tool_result`
handle with a `tool_output_preview_chars` preview, and the model reads only
the slices it needs. A `!command` typed while a turn is running waits its turn
in the queue, whatever the send mode; nothing is sent to the model until you
send a message, so `!make test` followed by `why did that fail?` is the usual
shape.
