# Desktop app (Electron) plan

Status: proposed implementation plan. Nothing here is built. Verify the current
code before extending it; the module names below were read from docstrings and
interfaces, not from a full code read. Third-party API details (Dockview options
in particular) are unverified and must be checked against the installed version.

## Goal

An optional desktop app for macOS and Linux that attaches to pcode sessions and
renders them with things a terminal cannot do:

- Output that glides in while streaming, instead of jumping a line at a time.
- Rich blocks: rendered markdown, highlighted code, inline diffs, collapsible
  tool calls.
- Splits and tabs, one session per pane, driven by a prefix key.
- One theme across chrome, code, diffs, and embedded terminals.

The terminal app stays the primary frontend and keeps its rule that the
transcript lives in the terminal's own scrollback (`PLAN.md`). The desktop app
is a second client of the same sessions, not a replacement.

## What already exists

The app is a new client of the session host, not a refactor of the core.

- `pcode.host` is a headless process that owns one conversation. Clients attach
  over a Unix socket; the host keeps working when they detach, and buffers the
  running turn so a client attaching mid-turn catches up.
- `pcode.rpc.Peer` is two-way newline-delimited JSON RPC (`notify`, `request`,
  `reply`). `pcode.host_protocol` holds the event types, the `hello`/`ready`
  version handshake (`PROTOCOL`), and the registry directory that lists running
  and stopped hosts without connecting to them.
- `pcode.controller` defines the surface: `SessionView` (what the host shows),
  `INTENTS` and `QUERIES` (what a client may ask), and `SESSION_FIELDS` (the
  mirrored busy/queue/prompt state).
- `pcode.gateway` starts hosts and drives them without a terminal. `--attach -p`
  and the email listener use it.
- `integrations/obsidian` is an existing TypeScript client, with its own build
  and tests. `dev/obsidian-plugin.md` records what testing it taught us.

So: do not add a `--rpc` stdio mode or a second event vocabulary. Electron's
main process connects to host sockets. That keeps sessions alive across app
restarts and lets a terminal and the app watch the same session.

## Decisions

- **macOS and Linux only.** The host uses `asyncio.start_unix_server`, which
  Windows lacks, and nothing in the Python side is tested there. Windows is a
  separate project that starts in the core.
- **One pane is one host.** No multi-session process and no `session_id`
  multiplexing; the registry directory is the session list.
- **No tool approval UI.** The host protocol has no per-call approval flow, so
  the app does not invent one.
- **Native scrolling.** The viewport is a normal scroll container. The spring
  drives `scrollTop` only while following new output. Wheel and trackpad input
  is left to Chromium, which is already smooth; putting a spring on it adds lag.
  No `transform`-based scrolling, which would mean rebuilding the scrollbar,
  find, selection autoscroll, and keyboard paging.
- **Reuse the prefix-key setting.** pcode already has a configurable prefix with
  a which-key menu (`prefix_keys.py`, the `key_prefix` preference). The app
  reads the same setting instead of hardcoding `Ctrl+A`, which collides with
  start-of-line in the editor and select-all on Linux.
- **The terminal pane is the parity fallback.** A pane running the real
  `pcode --attach ID` in xterm.js covers every feature the rich view lacks. It
  is part of v1, not an optional extra.
- **Uses the installed pcode.** The app runs the `pcode` on `PATH` and relies on
  the protocol handshake to refuse a mismatch. Bundling Python is deferred.

## Scope of the rich view in v1

`ui.py`, `app.py`, and the `*_ui.py` pickers are well over 10k lines. The rich
view does not chase parity. In v1 it does:

- Show the conversation: user prompts, streamed text and thinking, tool calls
  and results, edits as diffs, command output, plan updates, notes and errors.
- Send a prompt, queue one while busy, cancel a turn.
- Run slash commands that need no picker, showing their output as text.
- Show session state: busy, queue, model, usage.

Everything else (the tree, model and config pickers, jobs browser, side
questions, sign-in flows, vi editing, completion) is reached through a terminal
pane on the same host. Each one moves into the rich view only when someone
wants it enough to build it.

## Phases

Each phase ends with something usable and a reason to stop if it disappoints.

### Phase 0: scroll prototype

The premise is that streamed output can feel clearly better than a terminal.
Test that before anything else, with no backend work.

- A plain Vite page (no Electron) that replays a saved `transcript.jsonl` at
  recorded or accelerated pace into the stream viewport.
- The viewport: spring-driven follow, unpin, re-pin, a short fade on new blocks,
  and a paced queue for bursty text deltas that drains faster as it grows.
- Streaming markdown and code highlighting, since their reflow is what makes
  this hard.

Things the first spec got wrong or skipped, which the prototype must handle:

- The spring step must not depend on frame rate. Use the closed-form critically
  damped solution, or clamp `dt`; `requestAnimationFrame` pauses in a hidden
  window and resumes with a multi-second `dt`.
- Unpin on user input events (wheel, keys, scrollbar drag), not on `scroll`
  events, because the spring causes those itself.
- Height changes in both directions while streaming (a code fence opens, a
  table forms, a tool card collapses). Following must stay stable through it.
- Highlight only the block being streamed, and cache finished blocks.
  Re-highlighting the whole message per delta is quadratic.
- Long sessions need virtualization, which fights measured heights. Decide how
  (or whether v1 simply caps what is mounted) here, with a real long transcript.

Pass criteria, measured rather than eyeballed: replaying a long, tool-heavy
session at 4x, a Chromium trace shows no long frames attributable to the
viewport, and scrolling up during streaming never gets pulled back down. Then
the subjective check: if it does not feel clearly better than the terminal,
stop here.

### Phase 1: a client-ready host protocol

The protocol works for a Python terminal client. A TypeScript client needs:

- **A schema.** Events, view calls, intents, queries, and session fields
  described once, with TypeScript types generated from it and a test that fails
  when the Python side drifts. Today the Obsidian plugin hand-copies what it
  needs.
- **Structured tool data.** `ToolStarted.detail` and friends are strings
  formatted for a terminal. Check what `arguments`, `result`, and
  `EditCompleted.patch` already carry before adding fields.
- **History over the socket.** The terminal reads the journal from disk
  (`remote.HostedSession`) for replay, `/tree`, and `/diffs`. Add a query that
  returns the conversation so far as events, so the client does not parse
  `steps.sqlite3`.
- **A sort of view calls.** Mark which `SessionView` calls are data (render it)
  and which assume a terminal (`redraw`, `flash`, `run_command`, the async
  pickers). A rich client must be able to decline the second kind cleanly.

Any incompatible change bumps `PROTOCOL`. The terminal client and the Obsidian
plugin must keep working; that is the acceptance test.

### Phase 2: single-pane app

- Scaffold `desktop/` with `electron-vite`, React, and TypeScript. Context
  isolation on, no Node in the renderer, a narrow preload API.
- Main process: list hosts from the registry, start one through the same path
  `gateway.start_session` uses, connect to its socket, do the handshake, and
  forward messages to the renderer.
- Renderer: the Phase 0 viewport fed by live events, plus a prompt box with
  submit, queue, and cancel.
- **Environment.** A host inherits the env of whatever started it, on purpose,
  so it gets direnv credentials and `PATH`. An app launched from Finder has
  neither. Before spawning a host, resolve the environment for that workspace
  through the user's login shell and direnv, and show a clear error when no
  provider credentials result. Without this every session fails on its first
  request.
- Model output is untrusted. Sanitize rendered markdown, never inject raw HTML,
  and open links in the system browser.

### Phase 3: panes, keys, terminal pane, themes

- Dockview for splits and tabs, one panel type per pane kind (`session`,
  `terminal`), layout saved and restored.
- The prefix state machine: capture-phase `keydown`, match on `event.key` so
  `%` and `"` work across layouts, ignore events while `isComposing`, forward
  the prefix itself when pressed twice, disarm on Escape or timeout. Focus
  movement between splits probably needs our own geometry code.
- Terminal pane: `node-pty` and xterm.js running a shell or `pcode --attach ID`.
  `node-pty` is a native module and must be rebuilt for Electron's ABI.
- Themes: one token set as CSS custom properties, mapped onto Dockview's
  variables, the highlighter theme, and the terminal's 16 colors. The terminal
  app has no palette of its own: it derives chrome colors from the selected
  Pygments style (`syntax_colors.py`). Decide here whether the app follows that
  setting, so both frontends match, or keeps separate presets.

### Phase 4: packaging

- `electron-builder` targets for macOS (`.dmg`) and Linux (`.AppImage`).
- macOS signing and notarization.
- A first-run check that finds `pcode`, reports its version, and explains a
  protocol mismatch.
- Auto-update and a bundled Python are out of scope until someone other than us
  uses it.

## Testing

- Python protocol changes get ordinary pytest coverage, including the schema
  drift test.
- TypeScript logic (spring, pacing queue, prefix state machine, message
  decoding) is pure and unit-tested, following the split the Obsidian plugin
  uses between `core.ts` and UI code.
- UI needs a real run. The Obsidian plugin shipped a dialog with no text field
  while `tsc` and unit tests passed. Drive the built app with Playwright's
  Electron support against a host started from this checkout's venv.
- Scroll quality is checked by the Phase 0 trace criteria, rerun in the real
  app in Phase 2.

## Risks

- **Two frontends forever.** Every new feature either gets built twice or lives
  only in the terminal. The scope rule above (rich view shows the conversation,
  terminal pane does the rest) is what keeps this bounded; hold to it.
- **Protocol churn.** The host protocol changes often today because both ends
  ship together. A schema and a second client make changes more expensive.
- **Environment resolution.** Login shells and direnv are slow and varied. This
  is the part most likely to produce bug reports.
- **Streaming reflow.** If Phase 0 cannot make markdown reflow feel smooth, the
  main reason for the app is gone.

## Alternative considered

The harness ships an experimental ACP adapter
(`pydantic_ai_harness.experimental.acp`), which would put a pcode agent inside
Zed and other ACP editors with no UI work. It exposes a plain agent run, not
the session host, so queueing, side questions, jobs, workers, and attach would
be lost. Worth revisiting if the goal shrinks to "a rich view somewhere".
