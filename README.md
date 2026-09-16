# pcode

A small, scrollback-native terminal for a Pydantic AI Coder agent, with an offline
UI preview. See [PLAN.md](PLAN.md) for the longer-term direction.

## Run

With [uv](https://docs.astral.sh/uv/) installed, from this directory:

```sh
uv run pcode -m openai-codex:gpt-5.6-luna
```

`-m` / `--model` selects the Pydantic model/provider without remapping either name.
For `openai-codex:`, pcode constructs the native model with one profile override:
explicit prompt-cache breakpoints are disabled. Pydantic AI 2.43.0 advertises them
for this model family, but the subscription endpoint rejects the marker added by
Harness Planning after `write_plan` with HTTP 400. Authentication and streaming
still use the native provider, not a custom transport.

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
Live conversations save automatically. `/new` starts a new saved conversation
without deleting the old one.

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
`OPENAI_API_KEY` through your environment. Only the OpenAI provider extra is
installed by default. Other Pydantic model strings require their provider extras
and corresponding authentication.

### Tool permissions

**Live mode enables actual Coder file edits and shell tools. There is no approval
UI or sandbox yet.** Use a trusted repository and a safe working environment.
The agent is instructed to answer questions without changing files unless asked,
and to avoid credential contents, but instructions are not an enforcement boundary.

This project pins Harness 0.31.x. Its Coder composition includes filesystem,
shell, repository context, planning, an explorer subagent, and context management.
Its default command allowlist is not a sandbox: permitted interpreters/build tools
can run arbitrary code. Files and code returned by tools are sent to the selected
model. Background processes started by tools can outlive a turn; cancelling a run
is not an undo of completed tool effects.

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
a session for writing. `/sessions` lists sessions from inside the terminal too.

Default location: `$XDG_STATE_HOME/pcode/sessions`, or
`~/.local/state/pcode/sessions`. Override with `--session-dir PATH` or
`PCODE_SESSION_DIR`. Each session directory contains:

- `session.json`: model, workspace, timestamps, completed-turn usage, and package versions.
- `steps.sqlite3`: Harness `StepPersistence` events, full Pydantic message snapshots
  (including tool arguments/results and provider reasoning metadata), and a tool-effect ledger.
- `transcript.jsonl`: submitted prompts, streamed text, completed blocks/tool summaries,
  and structured failure diagnostics (HTTP status, provider code/parameter/message).

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
the most recent settled checkpoint. If a tool was in flight at a crash and its
outcome is unknown, resume refuses rather than risking a repeated side effect;
inspect the ledger before proceeding. Checkpoints do not restore files, running
processes, or capability-local state such as the in-memory planner.

Nothing from sessions run before this feature was installed can be reconstructed
from disk; those earlier conversations were memory-only.

## Offline preview and commands

```sh
uv run pcode                 # no model, canned replies only
uv run pcode --demo          # print a sample and exit, no terminal/auth needed
uv run pcode --theme light   # light input palette
```

Type `/` to open the command menu, then narrow it by typing. Use Tab or the
arrow keys to choose. Enter accepts a selected completion; another Enter runs it.

- `/demo`: fictional Markdown, code, diff, table, and tool summaries; never calls
  the model, even in live mode, and does not enter its conversation history.
- `/theme light` or `/theme dark`: change the input and future output palette.
  `/theme` alone toggles.
- `/help`: command list and keyboard shortcuts.
- `/context`: current model, workspace, completed turns, and token usage.
- `/new`: start a new saved conversation without clearing scrollback or input history.
- `/sessions`: list saved conversations and resume instructions.
- `/quit` (alias `/exit`): exit.

### Keys and layout

| Key | Action |
| --- | --- |
| Enter | Send, or accept a selected completion |
| Alt+Enter | Newline (Esc followed by Enter also works) |
| Tab / arrows | Browse completion; arrows also navigate input/history |
| Ctrl+R | Search this process's input history |
| Ctrl+C | Discard input, or cancel the running agent |
| Ctrl+D | Exit on empty idle input; cancel during generation |

The input is bottom-aligned from startup, with one editable line plus its border.
It expands upward for wrapped text or explicit newlines, and shrinks when text is
removed. Completion appears above the frame. Very long input scrolls within the
available pane height. Multiline bracketed paste works; mouse capture is off.

During generation, a small temporary region above the prompt shows live text or
current activity. Finalized text blocks become Rich Markdown in ordinary terminal
scrollback, printed once. Completed tools get concise summaries rather than raw
output dumps. The prompt is read-only during a run; cancellation restores editing.
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
- `src/pcode/ui.py`: prompt_toolkit editor, bottom-aligned layout, temporary live
  output, and Rich finalized transcript rendering.
- `src/pcode/commands.py`: registry shared by dispatch, help, and completion.
- `src/pcode/app.py`: CLI and asynchronous composition.

Rich owns permanent pixels; prompt_toolkit owns mutable pixels. Completed blocks
are printed through `run_in_terminal`, which suspends and restores the editing
area. No full-screen conversation viewport, alternate screen, custom cursor
positioning, or manually reserved scroll region. Existing transcript is never
repainted. Bottom placement relies on ordinary terminal cursor-position reports.

Approvals, queued prompts, model pickers, and MCP management are not implemented yet. Each run is capped at 30 model requests as a basic guard
against runaway tool loops, not a monetary budget.

## References

- [prompt_toolkit prompts](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html)
- [Rich Console](https://rich.readthedocs.io/en/stable/console.html)
- [Pydantic streaming events](https://ai.pydantic.dev/agents/#streaming-all-events)
- [Pydantic Harness Coder](https://ai.pydantic.dev/harness/coder/)

The latest Harness website describes a newer Coder composition than the pinned
0.31.x release. Implementation follows the installed release's public API.

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
and refusal to resume unresolved side effects. A native-provider wire test checks
that explicit cache markers are omitted while streaming/store settings are retained.
PTY tests check clean startup/exit
without alternate-screen or scroll-region sequences. When tmux is installed,
isolated-server tests measure prompt height and bottom placement through splits,
streaming, cancellation, and replies, and check transcript retention.

Real tmux tests include cursor-position reports: plain PTYs alone missed the
original frame-stretching bug. Actual copy-mode/search and rendering in your
terminal still deserve a manual feel check.
