# pcode

A small, streaming terminal for a Pydantic AI Coder agent, with an offline
UI preview. See [PLAN.md](PLAN.md) for the longer-term direction.

## Install with Homebrew

With [Homebrew](https://brew.sh/) installed:

```sh
brew tap aweis89/pcode https://github.com/aweis89/pcode.git
brew install --HEAD aweis89/pcode/pcode
pcode --demo
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
startups. `/effort` and Ctrl+N/Ctrl+P also save the selected reasoning effort
and current model. Preferences live in `~/.config/pcode/preferences.json`
(or `$XDG_CONFIG_HOME/pcode/preferences.json` when set), independently of saved
conversations and `--no-save`. Run `pcode` with no model argument to reuse the
saved model; without a saved default it opens the offline preview. The saved
effort applies to OpenAI/Codex, Anthropic, and Meridian models, including new and resumed conversations;
`/effort default` restores provider-default behavior. Use `pcode config unset KEY`
to reset an individual default. `--demo` always stays offline.

`-m` / `--model` overrides the saved model for that launch; `--resume` uses the
session's model. Neither changes the saved default by itself.

`-m` / `--model` selects the Pydantic model/provider without remapping either name.
For `openai-codex:`, pcode constructs the native model with one profile override:
explicit prompt-cache breakpoints are disabled. Pydantic AI 2.43.0 advertises them
for this model family, but the subscription endpoint rejects the marker added by
Harness Planning after `write_plan` with HTTP 400. Authentication and streaming
still use the native provider, not a custom transport.

### Global configuration

Global defaults are shared across workspaces in `~/.config/pcode/preferences.json`
(or `$XDG_CONFIG_HOME/pcode/preferences.json`). No migration or second config file
is needed. Inspect and edit them without opening a terminal UI or connecting a model:

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
```

The same commands are available inside pcode as `/config`, with tab completion:
`/config set theme light`, `/config get autocompact`, `/config unset effort`, etc.
**Config edits affect the next launch, not the running conversation.** To change
an active setting and save its default immediately, use `/theme`, `/effort`,
`/model`, or `/autocompact` instead. CLI overrides such as `--theme` and `--model`
do not rewrite global defaults, and resumed sessions retain their own model.

| Key | Built-in default | Values |
| --- | --- | --- |
| `theme` | `dark` | `dark`, `light`, `auto` |
| `autocompact` | `off` | `on`, `off` |
| `effort` | `default` | `low`, `medium`, `high`, `xhigh`, `default` (OpenAI/Codex, Anthropic, Meridian) |
| `model` | `null` (offline preview) | A model name, normally `provider:model` |

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
Opening the app, using commands, or quitting without a prompt creates no session.
`/new` resets context without deleting the old conversation; its replacement is
created on the next model prompt.

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
Both the OpenAI and Anthropic provider extras are installed by default, including
with `make install`. Run `make install` again to refresh an existing editable
installation after dependency changes. Other Pydantic model strings require their
provider extras and corresponding authentication.

### Reuse an existing pi Anthropic login

If you already logged in to Anthropic in pi, explicitly select that credential:

```sh
make install
env -u PCODE_LLM_PROXY PCODE_ANTHROPIC_AUTH=pi pcode -m anthropic:<model-id>
```

Alternatively, enter `/login` (or `/login pi`) in an idle Anthropic session to switch its current
model to pi authentication without discarding history. In offline preview this
checks the credential; launch with the environment setting above to use a live
model. There is no API-key entry UI or pcode-managed credential storage.
For ordinary API-key access, set `ANTHROPIC_API_KEY` in your environment.
Login is unavailable while a run or queued prompts are active.
For OpenAI Codex, continue to use `codex login`.

- Reads the `anthropic` entry in `~/.pi/agent/auth.json` at runtime. Honors
  `PI_CODING_AGENT_DIR` for a custom pi directory. No pi credential file is read
  unless you select pi authentication.
- Supports a stored OAuth access token or literal API key. Does not execute pi's
  shell-command/API-key expressions or resolve provider-specific environment maps.
- Pi selection overrides `ANTHROPIC_API_KEY`. Missing, invalid,
  or expired pi credentials produce an error, never a fallback to another account.
- Uses OAuth Bearer authentication with pi-compatible beta headers and system
  preamble; API keys retain ordinary API-key authentication. OAuth compatibility
  follows [pi's transport](https://github.com/badlogic/pi-mono/blob/main/packages/ai/src/api/anthropic-messages.ts),
  including its Claude Code wire identity markers. This is not an official
  third-party OAuth integration, and server compatibility/entitlements can change.
- Never copies credentials into pcode storage, changes pi's file, or uses its
  refresh token. Re-reads the access credential for every request/retry. If it
  expires, refresh it by using/logging in to pi, then retry in pcode. If pi changes
  credential type, run `/login pi` again or restart pcode.
- Sends model requests to `https://api.anthropic.com`; this adapter does not honor
  `ANTHROPIC_BASE_URL`. Billing and model access remain those of the pi credential.

Pi auth selection is process-local, not stored in sessions. Supply
`PCODE_ANTHROPIC_AUTH=pi` again when resuming in a new process (or export it in your
shell). Use `PCODE_ANTHROPIC_AUTH=api-key` or unset it for the normal environment API-key
flow. Existing environment settings are not overwritten in your shell.

### Choose a model in the terminal

Use **`/model`** or **Ctrl+L** to open the searchable model picker. Type to filter,
use ↑/↓ to select, and press Enter to apply. Filtering matches both provider and
model names, including joined word prefixes: `anthopus` finds Anthropic Opus,
`codluna` finds Codex Luna, and `opus anth` works too. Escape, Ctrl+C, or Ctrl+L closes the
picker without changing the model or editor draft. For a model not in the catalog,
type its full `provider:model-id` (for example `anthropic:claude-opus-5`).

The picker currently supports configured **Anthropic** and **OpenAI Codex** providers:

- The current provider is included even when using a custom model ID.
- Anthropic is enabled by `ANTHROPIC_API_KEY`, `PCODE_ANTHROPIC_AUTH=pi`, or `/login`.
  Pi reuse remains opt-in; opening the picker never reads pi credentials.
- Codex is enabled when its CLI credential file exists (`CODEX_HOME` is honored).
  Opening the picker checks file presence only, not its contents or validity.
- `PCODE_LLM_PROXY` applies only to Codex and does not restrict model selection.

Models are grouped by provider and family, with numeric versions sorted newest
first (Opus 5 before Opus 4.8; 4.10 before 4.9). Filtering preserves that order.
The current model is marked, not pinned above newer versions; undated aliases
precede dated snapshots of the same version. This uses model IDs, not release-date
metadata across different families.

Suggestions come from the installed Pydantic AI catalog (Anthropic models and
all OpenAI model IDs for Codex). Opening the picker makes **no network requests**.
This is not an account-entitlement list: the provider checks model
availability and credentials when you use the model. Custom IDs are accepted only
for providers enabled in the picker. If none are configured, use `/login`, set
`ANTHROPIC_API_KEY`, or run `codex login` first.

**Changing models continues the current conversation.** Message history, session ID,
plan, tool panel, usage totals, transcript, and editor draft are preserved. The
saved session records the selected model so resuming uses it too. Model-specific
settings are rebuilt for the selected model. Use `/new` to start over instead.
Selecting the current model is a no-op; `--no-save` still applies, and sessions
are saved lazily on their first prompt. A failed switch leaves the old conversation
intact. Model selection is disabled while a run or queued messages are active.
This also works from offline preview to start a live conversation without
restarting pcode.

### Local Meridian provider

Use your running [Meridian](https://github.com/rynfar/meridian) proxy as a separate
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

**Thinking visibility:** `/show-thinking on` only controls pcode's local preview.
Meridian must also forward thinking blocks. In the proxy's `/settings` page
(default: <http://127.0.0.1:3456/settings>), inspect the **passthrough** adapter's
**Thinking Passthrough** option. Meridian 1.71.1 defaults this to off. Enabling it
changes that proxy adapter for other clients too, so pcode does not modify it
automatically. Forwarding is separate from enabling model thinking or setting
effort; a model that emits no thinking blocks still has nothing to preview.

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

Set `EXA_API_KEY` in the environment before starting pcode to enable Exa-backed
`web_search` and `get_page` tools for the coder. The key is read by the Exa client,
not passed to the model. Without a nonblank key, search tools are omitted and
ordinary coding sessions work as before. The read-only explorer stays local.

Search returns up to five results with excerpts and source URLs; page retrieval
returns up to 10,000 characters. Deep search is disabled. Queries and requested
URLs are sent to Exa and may incur API charges; returned content is sent to the
model and can be saved in session history. Restart pcode after changing the key,
including when resuming a session.

### Tool permissions

**Live mode enables actual Coder file edits and shell tools. There is no approval
UI.** pcode does not implement its own permission model and does not use prompt
text as a safety control. Permission management is out of scope: run pcode inside
a sandboxing wrapper (a container, VM, or an OS sandbox such as `sandbox-exec`
or `bwrap`) when you need enforcement, and otherwise use a trusted repository and
a safe working environment.

File tools are scoped to the selected workspace by Harness's `FileSystem`
capability: paths resolve relative to the workspace root, traversal above it is
rejected, and protected patterns such as `.git/`, `.env`, `*.pem`, `*.key`, and
`**/secrets*` are read-only through these tools. The explorer subagent shares that
root but exposes only read-only file tools. Shell commands run with the workspace
as their working directory but are not confined to it: they can still read or
modify anything the OS allows, including files protected by the file tools.

This project pins Harness 0.31.x. Its Coder composition includes filesystem,
shell, repository context, planning, an explorer subagent, and context management.
pcode clears Harness's default command allowlist, so `run_command` accepts any
command: treat it as arbitrary code execution as the invoking user. Files and
code returned by tools are sent to the selected model. Background processes
started by tools can outlive a turn; cancelling a run is not an undo of
completed tool effects.

## Sessions and debugging

```sh
uv run pcode --sessions
uv run pcode --resume latest
uv run pcode --resume SESSION_ID
uv run pcode -m openai-codex:gpt-5.6-sol --no-save  # opt out for a sensitive session
```

Resume accepts an unambiguous ID prefix (at least 8 characters) and restores the
saved model, workspace, and structured message history. It prints recent transcript
blocks and waits for your next message; it does not automatically re-run tools.
A different explicit `-m` or `-C` is rejected on resume. Only one process may open
a session for writing. `/session` opens a popup of saved conversations in the current
workspace, labeled by their first prompt (newest first). Use ↑/↓ and Enter to
resume in place, or Esc to cancel. Resuming restores the saved model, history,
and plan.

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

Nothing from sessions run before this feature was installed can be reconstructed
from disk; those earlier conversations were memory-only.

## Offline preview and commands

```sh
uv run pcode                 # no model, canned replies only
uv run pcode --demo          # print a sample and exit, no terminal/auth needed
uv run pcode --theme light   # light input palette
uv run pcode --theme auto    # detect terminal background at startup
```

Type `/` to open the command menu, then narrow it by typing. Use Tab or the
arrow keys to choose. Enter accepts a selected completion; another Enter runs it.

- `/demo`: fictional Markdown, code, diff, table, and tool summaries; never calls
  the model, even in live mode, and does not enter its conversation history.
- `/theme light`, `/theme dark`, or `/theme auto`: change the input and future output palette.
  Auto uses the terminal background detected at startup with an OSC 11 query,
  falling back to `COLORFGBG`, then dark when unavailable (including redirected
  output). Restart pcode after changing your terminal background. Save auto mode
  with `/theme auto` or `pcode config set theme auto`; the built-in default remains dark.
  `/theme` alone toggles. By default, Rich headings, links, quotes, inline code,
  and tables follow this palette; fenced code uses `nord` (dark) / `friendly`
  (light). Normal body text and the overall background remain terminal-native.
- `/colors terminal`: opt into terminal-defined ANSI colors with unpainted code
  backgrounds and `ansi_dark` / `ansi_light` syntax. `/colors palette` restores
  the default coordinated palette; `/colors` shows the current selection.
  This affects Rich output, not the input/completion palette. You can also start
  with `--color-style terminal` (default: `--color-style palette`). Run `/demo`
  after switching to compare headings, links, quotes, tables, Python, and diffs.
  Existing scrollback is not repainted.
- Session, conversation-tree, model, and tool popups share terminal-default
  backgrounds and text, with reverse-video selection highlights. They follow your
  terminal background automatically, independently of `/theme` and `/colors`.
- `/help`: command list and keyboard shortcuts.
- `/model`: searchable model picker for configured providers (keeps the conversation).
- `/tools`: scrollable tool-call inspector for the current conversation, including resumed calls.
- `/tools failed` or `/errors`: open the same inspector filtered to failures.
- `/context`: current model, workspace, completed turns, and token usage.
- `/compact [focus]`: summarize older context with the current model; keep recent history.
- `/autocompact on|off`: opt into automatic LLM compaction (saved user preference; default off).
- `/new`: start a new saved conversation without clearing the on-screen transcript or input history.
- `/session`: choose a saved conversation by its first prompt and resume it in place.
- `/tree`: [browse and fork the conversation](docs/conversation-tree.md); select a user prompt to
  edit it, or an assistant response to continue from there. Existing branches are kept.
- `/quit` (alias `/exit`): exit.

### Keys and layout

| Key | Action |
| --- | --- |
| Enter | Send (queue during generation), or accept a selected completion |
| Ctrl+J | Newline (map Shift+Enter to this in your terminal) |
| Alt+Enter | Newline in Emacs mode only (Esc followed by Enter also works) |
| Tab / arrows | Browse completion; arrows also navigate input/history |
| Ctrl+L | Choose a model (idle only; keeps the conversation) |
| Ctrl+R | Search this process's input history |
| Ctrl+S | Mirror commands and their output to scrollback (saves the default) |
| Ctrl+C | Discard idle input; during generation, cancel without deleting the draft |
| Ctrl+D | Exit on empty idle input; cancel during generation |

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
pcode setting; verify it inside tmux too if you use it.

Vi mode uses a 100 ms terminal escape-sequence timeout and an eager Escape binding.
This avoids waiting for an Alt-key chord before entering normal mode; particularly
slow or fragmented terminal connections may need a longer timeout in future.

Restore the default with `pcode config set editing_mode emacs` or
`pcode config unset editing_mode`.

The input is bottom-aligned from startup, with one editable line plus its border.
It expands upward for wrapped text or explicit newlines, and shrinks when text is
removed. Completion appears above the frame. Very long input scrolls within the
available pane height. Multiline bracketed paste works; mouse capture is off.

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
The non-interactive `--demo` sample still prints its fictional tool summaries.

### Tool-call inspector

Use `/tools`, `/tools failed`, or `/errors`, including during an active turn.
The inspector shows a snapshot of the calls available when opened; reopen it to
see newer results. The model keeps running while the inspector is open, and
terminal output is buffered until it closes. Inspection never reruns a tool.

- Calls are newest first. Use arrows to select and Tab/Shift+Tab to move between
  the call list, detail pane, and search field.
- In the call list, **f** toggles failures, **t** cycles tool-name filters, and
  **/** focuses search. Search matches tool names/statuses and command/summary
  previews, not the complete output payload. Ctrl+F focuses search from any pane.
- In details, use arrows, PageUp/PageDown, or Ctrl+Home/Ctrl+End to scroll.
- Mouse clicks and wheel scrolling work in the popups. In tmux, enable mouse
  forwarding with `tmux set -g mouse on` (or `set -g mouse on` in `~/.tmux.conf`).
- Escape, Ctrl+C, or Ctrl+D closes only the inspector and restores the editor draft.
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
with rendered Markdown. Tool summaries live inside the task widget. `/demo`
and restored session messages still use Rich Markdown.

The editor remains usable throughout generation, including multiline input,
history, and slash completion. Enter queues the next message and clears the
editor for another draft; the toolbar shows the queue count. Queued messages run
in order after the current turn finishes. Slash commands use a separate async
handler, so help, inspection, theme, context, and effort controls remain available
while the model works. `/new`, `/session`, `/login`, and `/model` require an idle conversation: cancel
or wait, then retry. `/quit` (or `/exit`) cancels the active run and waits for its
cleanup before exiting. Ctrl+C or Ctrl+D cancels the current turn, clears queued
messages, and preserves the unsubmitted draft and cursor. A failed turn also
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
cannot retroactively update earlier output. `--demo`
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

Core events have no Rich or prompt_toolkit dependencies. Raw reasoning tokens are
not transcript content: the stream adapter currently reduces thinking events to a
transient `RunStatus("Thinking…")`, owned by the live UI. Future small/expanded
thinking views should remain there; any intentionally retained reasoning summary
must be distinct from the raw stream. Plans, status, dialogs, and editor state also
remain mutable, outside `Transcript`.

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
`/context` continues to show cumulative session input/output usage.

## Reasoning effort

For OpenAI/Codex, Anthropic, and Meridian models, use **Ctrl+N** to increase effort and **Ctrl+P** to
decrease it, or `/effort low|medium|high|xhigh`. `/effort` shows the current
setting; `/effort default` removes the override. Slash completion includes these
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

Server names start with a letter and contain letters, digits, `_`, or `-` (up to
32 characters). Unsupported server fields are rejected on enable rather than
silently ignored. String values support `${VARIABLE}` and `${VARIABLE:-default}`.
Only the selected server's variables are expanded, at enable time, so missing
credentials for an unused server do not block ordinary work. Keep secrets in the
environment rather than the JSON file.

### OAuth sign-in

Remote servers can use the browser-based OAuth support built into Pydantic AI and
FastMCP. No separate auth tool, Pi token import, or custom OAuth flow is needed:

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
- Pi's `"auth": "oauth"` server definitions are compatible, but Pi's saved OAuth
  credentials and approvals are not imported. This is a separate authorization.
- Do not combine OAuth with an `Authorization` header. Non-auth headers may be used
  alongside OAuth. For a static bearer token, continue using `headers` with an
  environment variable reference instead of `auth`.
- Disabling a server drops pcode's reference to its OAuth client; it does not revoke
  the server-side grant. Revoke access through the service if needed.

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
  tools are namespaced as `mcp_NAME_TOOL`; their schemas and results consume
  context normally. Disabling does not erase earlier tool results from history.
- Enabling authorizes the agent to use the server's tools with that server's
  permissions, including write actions. There is no additional per-call approval
  or sandbox. Server instructions are not automatically added to the prompt.

## Validate

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

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
Summaries are lossy: original tool results remain available through the session/tool
history, and the model should re-read source files when exact details matter.

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

### Transient reasoning summary

Press **Ctrl+T** to show or hide the provider's visible reasoning summary.
The default **compact** view places the latest heading next to the task/tool
header, for example `Tasks 0/3 · Locating root evidence`. Markdown heading/bold
markers are removed and long text is clipped to the available terminal width.
When no task/tool panel exists yet, the summary uses a single unbordered line
above the current prompt. It does not add a separate box or take a task row.

These are provider-exposed summaries, not raw internal reasoning tokens. They
may be empty, a short heading, or several paragraphs. The compact view prefers
the latest heading in the current thinking block, falling back to its latest
nonempty line. A new nonempty block replaces the compact label; signature-only
blocks do not erase it. The **expanded** view retains the complete visible text
within its bounded rolling buffer and follows the latest wrapped lines:

```sh
pcode config set thinking_display compact   # Default: summary beside Tasks/Tools
pcode config set thinking_display expanded  # Opt into a separate multiline box
pcode config set thinking_lines 10          # Expanded view's maximum content rows
```

The expanded box sits above the current prompt/tasks, preserves paragraph breaks,
grows with content, and shrinks in small panes to leave room for tasks and input.
The line limit excludes borders. These settings also work through `/config` and
apply on the next launch; they do not request more verbose provider summaries.

`/show-thinking on` and `/show-thinking off` change current visibility and save the
default; `/show-thinking` reports it. `pcode config set show_thinking on` sets the
startup default (`off` by default). Ctrl+T changes visibility immediately.

Visible summary text goes directly to bounded, mutable prompt_toolkit UI state,
not through transcript events or permanent terminal output. It is cleared on
completion, cancellation, failure, and conversation reset. Hiding the view keeps
the current turn's buffers so it can be shown again. Each buffer is bounded to
8,192 characters. This is a live preview, not a scrollable reasoning transcript.
It does not change the selected reasoning effort or existing saved model-message
history. As with other normal-screen panels, aggressive tmux resizing can move
old widget rows into terminal history before pcode can erase them.

For direct Anthropic models, enabling the view also requests thinking on the
next turn (adaptive where supported, otherwise a 2,048-token budget). This can
increase latency and token usage. Turning it off removes that request and
restores provider defaults; an in-flight request is unchanged. Codex requests
provider-exposed reasoning summaries independently of visibility. Meridian still
requires thinking generation upstream and **Thinking Passthrough** in its proxy
settings; pcode does not change the proxy's global settings.

### Error logs in scrollback

Errors and failed-tool diagnostics render as fenced Markdown code blocks using
Rich and the active code theme. Logs stay literal, even if they contain Markdown
or backticks. By default, each error shows at most **20 wrapped body lines**, plus
its heading and two code-block padding rows. Long logs keep their tail and include
a truncation marker within that limit.

```sh
pcode config set error_scrollback_lines 40  # Positive integer; default 20
pcode config set error_scrollback off      # Hide error blocks entirely
pcode config set error_scrollback on       # Restore error blocks (default)
```

These settings also work through `/config` and apply on the next launch. They
affect only error scrollback, including replayed failed-tool events; warnings and
cancellation notices remain visible. Saved diagnostics are not disabled or trimmed
by these display settings. Command diagnostics retain a separate safety bound of
200 lines / 32,000 characters, after redaction.

### Command output in scrollback

By default, successful tool calls stay in the mutable tool panel and only
failures reach scrollback. Enable `command_scrollback` to mirror **every settled
shell tool call and its captured output** into permanent terminal scrollback:

```sh
pcode config set command_scrollback on        # Mirror commands and output (default off)
pcode config set command_scrollback_lines 80  # Positive integer; default 40
pcode config set command_scrollback off       # Restore failure-only scrollback
```

Each mirrored block shows the tool label, elapsed time, the invocation on a `$`
line, and the tool's captured output in a fenced literal code block:

```text
› Run · 0.4s
  $ pytest -q
  2 passed in 0.31s
  [exit code: 0]
```

Details:

- It covers `run_command`, `start_command`, `check_command`, and `stop_command`,
  including calls made by delegated sub-agents. Other tools are unaffected.
- Blocks are written when a call settles, not incrementally while it runs, so
  output appears once per call rather than line by line.
- Failed commands print one mirrored block containing the full captured output
  instead of the shorter `error_scrollback` excerpt; `error_scrollback` still
  governs every other failure.
- Output is redacted and sanitized before display, then bounded to
  `command_scrollback_lines` wrapped body rows, keeping the tail with a
  truncation marker. The capture step retains its own 128 KiB payload bound.
- Verbose commands can push earlier conversation out of terminal history, so
  raise your terminal or tmux scrollback limit before enabling this.

Press **Ctrl+S** to turn mirroring on or off for the rest of the session; it also
saves the default, so the next launch starts in the state you left.
`/show-commands on` and `/show-commands off` do the same, and `/show-commands`
reports the current state. A toggle applies to the next command that settles;
scrollback already written is never rewritten.

Ctrl+S replaces prompt_toolkit's forward incremental search. Ctrl+R still opens
history search, which is the search binding this terminal documents. prompt_toolkit
disables terminal XON/XOFF flow control while the prompt is active, so Ctrl+S
reaches the application instead of pausing terminal output.

These settings also work through `/config` and apply on the next launch.
