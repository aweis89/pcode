# Development

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
them. They run in parallel like the rest, and beside other worktrees' runs,
because each test boots a private tmux server and none of them races the clock.
Keep new ones that way with the helpers in `tests/test_tmux.py`:

- Wait with `capture`, `settle`, or `until`. They poll the pane and give up
  only after a generous `TIMEOUT`, so load slows a test down instead of
  failing it.
- A scripted turn that the test inspects mid-flight holds on `await gate()`
  until the test calls `release()`, never on a fixed `sleep` the test has to act
  inside.
- Resize through `resize(pane, ...)` before asserting on a transcript note. It
  waits for the debounced scrollback replay, which erases notes already on
  screen.

Run `make test-all` before pushing anything that touches layout, streaming, or
the editor.

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
resize.

Real tmux tests include cursor-position reports: plain PTYs alone missed the
original frame-stretching bug. Actual copy-mode/search and rendering in your
terminal still deserve a manual feel check.

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
- `src/pcode/turn.py`: the state one turn owns (history, queued shell exchanges,
  plan store, request checkpoint, in-flight context). The runtime holds the active
  branch's and exposes its fields under their original names; per-run capabilities
  bind to the context they were created for, not to the runtime.
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

Approvals are not implemented. Model request-count limits are explicitly
disabled; there is no monetary budget guard.

## Offline preview

`uv run pcode` with no model and no saved default runs `PreviewRuntime`
(`src/pcode/runtime.py`): canned replies and fictional tool summaries, no host
process, no API keys. It exercises the editor, popups, `/tools` (which shows the
preview's own fixtures), and themes without a model call.

```sh
uv run pcode                 # no model, canned replies only
uv run pcode --theme-preview # print a sample and the style gallery, then exit
uv run pcode --theme light   # light input palette
uv run pcode --theme auto    # detect terminal background at startup (OSC 11)
```

## Docs screenshots

`make screenshots` regenerates `docs/assets/screenshots/*.svg` from scripted
scenes: the real terminal on a scripted model in a private tmux server. See
`scripts/screenshots/scene.py` for the format and the `screenshots` skill in
`.agents/skills/` for how to add one. Rerun it after a UI change and review the
image diff before committing.

## Session hosts

Every interactive session with a model runs in a host process. `SessionController`
(`src/pcode/controller.py`) owns the session (queues, send modes, turns, commands,
MCP, side questions, jobs) and runs in the host behind `HostView`; the terminal
attaches over the socket protocol with a `RemoteController` and runs only its own
commands (`TERMINAL_COMMANDS`). `--no-host` and `--print` run a controller
in-process with `PreviewApp` as its view. The user-facing side is
[background sessions](../docs/sessions.md#background-sessions).

Each host is its own process listening on a Unix socket. An attaching terminal
is sent the conversation so far, then every event as the turn produces it, which
is how switching to a session mid-turn resumes streaming text and running
commands in place.

Traps:

- Unix socket paths are capped at 104 bytes on macOS. pytest's `tmp_path` is too
  long, so tests put hosts under a short `/tmp` directory (`PCODE_HOST_DIR`).
  `/tmp` resolves to `/private/tmp`; compare resolved workspaces.
- asyncio's stream line limit defaults to 64 KiB; one tool result is larger.
  Readers use `LINE_LIMIT`.
- `EditPreview` has a `kind` field, so events are encoded as `{"kind", "fields"}`,
  not spread beside the name the way the journal writes them.
- A running turn is partly in the journal (settled steps) and partly not (streaming
  text, previews, live command output). A snapshot reads the journal only up to
  where the turn began (`SavedSession.records(end)`) and sends the turn from the
  host's buffer.
- Cancel-then-prompt races: the host must not clear a queue while a cancelled turn
  unwinds, and a terminal must detach its inbox only on the `turn_finished` of the
  prompt it sent.
- A `Mock` runtime in tests answers every attribute, so feature checks on the
  runtime use `is True` rather than truthiness.
- Hosts inherit the environment of the terminal that spawned them. One shared
  daemon could not: `os.environ` is per process, and pcode reads credentials from it.

`tests/test_session_host.py` covers the wire format, host logic, and switching
away mid-turn and back over a real socket. `make test-socket` reruns the fast
suite with each session in a host (see `AGENTS.md` for what that changes).

## One turn at a time

Two conversation turns never run in parallel, and much of the code assumes it:
the conversation tree records events against a single "recording" cursor, the
runtime keeps one history, plan store, request checkpoint and shell, the session
journal is a single append-only stream, and the transcript and activity widget
present one stream of tool and text events. `/btw` side questions fit inside
that because their answers land in their own surface and they cannot touch the
plan or start workers. This is also why `/tree` is read-only while a turn runs.

## Worktrees from the Makefile

`make worktree NAME=...` (and the other `python -m pcode.worktree` targets) uses
the name as given, without the `pcode-` prefix session worktrees get, so a
session's exit cleanup treats such a worktree as hand-made and only reports it.
`make worktree-clean` runs `/worktree clean` from the main checkout, and
`make clean-merged` follows it by deleting branches the default branch already
contains, locally and on origin (`ARGS=--dry-run` lists them first). It is the
fix for merged branches left behind by worktrees removed by hand, since
`git branch -d` refuses a branch a worktree still holds.

## Resource profiling

Use `pcode --profile` to sample pcode and its child processes' CPU, resident
memory, and thread counts while reproducing a resource problem, then
`python -m pcode.profiling DIR` to read the capture. Samples record whether a
turn was running, so the report separates working cost from idle cost. Bare
`--profile` writes a timestamped directory under `~/.local/state/pcode/profiles`
and prunes old ones; `--profile DIR` names its own, which must be new.
`summary.json` is republished after every sample, so a session that is killed
still leaves its totals. Nothing is collected by default;
`pcode config set profile resources` captures every session and `--no-profile`
opts one run out.

For a short detailed capture, add `--profile-cpu` (function CPU time across Python
threads) or `--profile-memory` (Python allocation locations and traced memory peaks).
These add substantial overhead, so use separate runs and resource-only captures for timing.
Captures have private permissions, but can contain local source paths; inspect
before sharing. See [profiling and the optimization plan](profiling.md) for
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

## References

- [prompt_toolkit prompts](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html)
- [Rich Console](https://rich.readthedocs.io/en/stable/console.html)
- [Pydantic streaming events](https://ai.pydantic.dev/agents/#streaming-all-events)
- [Pydantic Harness Coder](https://ai.pydantic.dev/harness/coder/)

The latest Harness website can describe a newer Coder composition than the pinned
release. Implementation follows the installed release's public API
(`make harness-src` checks out the locked release).
