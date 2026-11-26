# Native app plan: making Ghostty a leaf in Pcode.app

Status: steps 1, 2 and 3a done.
Steps 3b and 4 are undecided: do not start them. This came out of a code
survey, not a full design; check the current code before extending any step.

## Goal

Pcode.app should be a native macOS client of pcode sessions, with the session
host as its spine and Ghostty reduced to a view that hosts a terminal where a
terminal is still wanted.

The native AppKit transcript has replaced the terminal render as the way a
session is shown, and it is nicer and richer than the terminal view. What is
left of Ghostty in the app is mostly plumbing: the app shell, a hidden terminal
behind the transcript, and terminal surfaces for the prompt editor, popups and
`/btw`. Steps 1 to 3a remove the plumbing that nobody sees. They are worth
doing whether or not Ghostty stays.

Whether the app drops Ghostty altogether (a native composer, then its own
container) is an open decision, recorded as steps 3b and 4. Keeping Ghostty for
the editor and the shell split is a valid end state.

The Ghostty fork (`aweis89/ghostty-smooth`) stays as the standalone terminal
for people who use pcode's terminal UI either way. pcode's terminal mode must
keep working unchanged through every step: these changes are for the Swift app
only.

## What Ghostty does in the app today

- App container. The fork owns the Xcode project, `AppDelegate`, the menu,
  config (theme, fonts, keybinds) and the debug socket that `scripts/gdbg.py`
  drives. Our Swift is copied in through the `PCodeAppHooks` seam.
- The hidden terminal behind the transcript. Each pane's surface runs the pcode
  TUI as scrollback only (`PCODE_DOCK=1`), with the native transcript mounted
  over it in `PCodeDock.swift`. It gives the pcode process a pty and carries
  the `pcode;...` APC messages.
- The composer. The whole prompt_toolkit editor, task list and status bar run
  on a second surface (the dock). Popups that are not native pick lists open as
  more terminal overlays, and `/btw` is another surface running
  `pcode.btw_tail`.
- The bottom shell split. This is the only real terminal use.

Size of the coupling when surveyed: 16 of 28 Swift files reference Ghostty
(`PCodeWorkspace.swift` alone 71 times). The fork is 137 commits and about
6,350 lines ahead of upstream, of which about 4,450 are the Zig glide renderer
that the app no longer shows anyone.

The terminal is the spine of a pane today, not a leaf:

```
pane's hidden Ghostty surface   runs the pcode TUI; owns the session's pty
   - draws a transcript nobody sees
   - carries pcode;... APCs (session id, toasts, popups, drawer, dock rows)
   - hands its editor to a second surface through the dock bridge (a spike)
native transcript               laid on top; reads the host socket or the journal
```

The session's life is tied to a surface the user never looks at, and the app
learns about its own session from escape codes in that surface's output.

## Steps, in order

### 1. Remove the terminal-render toggle (done)

The native transcript is the only session view. `PCodeNativeTranscriptMode`,
`PCodeUISurface`, `toggleLiveTranscript`, the Option-Cmd-T shortcut and button,
the debug `transcript` command and the old side-by-side
`PCodeLiveTranscriptPaneView` are gone, and panes always get
`PCODE_NATIVE_TRANSCRIPT=1`. Merged in `a1f4acef`, together with a fix for
copying text out of the native transcript.

The terminal surface behind the transcript is still there, because the pcode
process still needs a pty. The raw terminal can still be seen for a few seconds
at startup before the dock is ready, in a pane whose pcode has exited, and in
split panes. Step 3a removes that surface.

### 2. Move `/btw` to native (done)

`btw_tail` already reads the thread from the host socket, so a native drawer
can do the same and reuse the transcript block views. This removes one terminal
surface. The terminal-mode `/btw` path stays as it is. Merged in `b00c60ae` and
`ba8e2c96`.

### 3a. The app talks to the session host; the editor becomes a leaf (done)

Make the session host own every session the app shows, and have the app read
session state from the host socket. The terminal editor stays, as a client of
the host on one visible surface:

```
session host (headless)         owns the session
   - native transcript          host socket (works this way already)
   - native /btw                host socket (step 2)
   - editor                     one visible terminal surface, attached as a client
```

What this buys even with Ghostty still in the app:

- One surface per pane instead of two. The hidden pane surface and the dock's
  tty handoff go away.
- No wasted rendering. pcode stops drawing a full transcript into a terminal
  nobody sees.
- App state stops depending on the APC channel. Session id, activity and title
  come from the socket. APCs shrink to what the editor surface itself needs
  (popups, completions, toasts).
- Ghostty becomes swappable. Once it only hosts "a terminal that runs the
  editor" and "a terminal that runs a shell", replacing either is a local
  change, not a rewrite of `PCodeWorkspace.swift`.

What it does not buy: the app is still built on the fork's app shell, the Zig
toolchain and the unstable embedding API, and terminal popups (the `*_ui.py`
pickers) stay as overlays on the editor surface.

What has to be built:

- An editor-only pcode frontend: a mode where pcode attaches to a host and runs
  just the editor, task list and status bar on its own terminal, with no
  scrollback. `dock.py` is most of this already, minus the handoff. It is still
  marked as a spike and has no tests.
- A two-way Swift host client. `PCodeHostClient.swift` is read-only today
  (`observe_transcript` v1). For 3a it mostly needs state mirroring
  (`SESSION_FIELDS` in `pcode.controller`), since the terminal editor still
  does the submitting. `integrations/obsidian` is a working reference client.
- App-launched sessions always hosted. Today a new pane runs pcode in-process
  and only moves into a host when the app restarts; the transcript falls back
  to tailing the journal for those.

Main unknown: how much of the editor assumes it shares a process with the
transcript renderer. Survey that before sizing the work. The states listed
under step 1 where the raw terminal shows need a native answer here, since the
surface they fall back to is the one being removed.

#### 3a survey findings (2026-11-25)

The main unknown is mostly answered: the editor does not need the transcript
renderer in its process, but it builds the task list, tool rows, worker rows
and command/edit previews client-side from the transcript events it receives
(`present_stream_event` both prints and updates `Activity`). So an editor-only
client must keep processing view calls and send their output to a null sink.

Already there, no protocol change needed:

- Terminal host clients exist: `pcode --host` spawns a host and attaches,
  `pcode -c <id>` attaches when a host holds the session (`RemoteController`
  in `remote.py`). Both run the same `PreviewApp`, so `PCODE_DOCK` works there.
- `observe_transcript` already forwards `state` (`SESSION_FIELDS` deltas:
  busy, prompt state, turn outcome, queue) and `session_changed`; the Swift
  decoder ignores them. `hosts/<id>.json` is rewritten on every change and
  carries title, state, session id, unseen and outcome.
- Status bar, completions, drafts, history, image paste and the `/btw` thread
  already work over the host protocol. Popups, native pick, completions and
  toasts are APCs on the editor's own tty, so they keep working from whichever
  surface runs the editor.
- No Ghostty fork (Zig or seam) change is required. Several seam hooks become
  dead (`clickableHeight`, `clicksThroughOnFocus`, `allowsMouseFocus`,
  `didMouseUp`, `willKeyDown`); removing them from the fork is optional.

What has to change:

- Editor-only frontend (Python, gated on a new app-only env var such as
  `PCODE_EDITOR_ONLY=1`): a `Dock` variant on its own stdin/stdout with no
  `open_dock` handoff, pane forwarding, scroll region, CPR or glide, plus a
  null sink for `Transcript`/`TerminalOutput` writes, replay, `/redraw` and the
  full-screen popup fallback's redraw. Terminal mode never sets the variable.
- Popup, completion and toast layers are mounted per surface by the
  `surfaceOverlay` hook; on a card-sized editor surface they would be clipped.
  Mount them at pane level in `PCodeSessionPaneView` instead (Swift only).
- Native states for what the hidden terminal shows today: startup output
  (project-trust prompt, "needs a model", config errors), no session id yet,
  host stopped/reconnecting (replacing the journal-tail fallback), session
  ended. `startup_pending`/`startup_error` already exist host-side.
- Peek's scroll shield forwards to the hidden `pane.surface`
  (`PCodePeek.swift`); repoint it at the transcript.

Unverified: whether a clipped, fixed-grid editor surface sits well inside the
fork's split tree; host boot latency; what SIGUSR2 (Restart) does to a hosted
client.

Decisions for the "always hosted" piece:

- Closing a pane stops its host (owner, 2026-11-25), as closing ends the
  session today; it does not detach. The app picks the host's id up front
  (`PCODE_HOST_ID`), so a pane closed before its host reports itself still
  stops it.
- No model configured: `pcode --host` starts a host in preview mode with no
  model (`PreviewRuntime`) until `/model` activates an agent (owner, 2026-11-25),
  so every app pane runs hosted on a single editor-only surface and the app's
  two-surface dock handoff is removed. Only an explicit `--host` does this: in a terminal,
  `session_host=on` with no model still runs the canned preview in-process
  (owner).
- Startup output: until `pcode;ready` the pane's surface fills the pane
  unclipped, so the trust prompt and startup errors show at full height; a
  pcode that exits non-zero before its editor started waits for Enter
  (`dock.hold_failed_start`) instead of the pane vanishing.

#### 3a sequence

Each piece merges to `master` on its own and leaves the app working; the old
two-surface path stays the default until piece 6.

1. Swift: decode `state` and `session_changed` in `PCodeHostEventDecoder` and
   publish busy/outcome on the transcript controller. Unit tests; no consumer.
2. Swift: host-sourced pane state. Watch `hosts/<id>.json` for title, outcome
   and session id; pane activity and title prefer host data when hosted and
   fall back to OSC/APC otherwise.
3. Python: editor-only frontend behind `PCODE_EDITOR_ONLY`, with tests that
   nothing outside the editor layout reaches stdout and that tasks/tool rows
   still update.
4. Swift: move popup, completion and toast layers to pane level, still fed by
   the dock surface. No visible change.
5. Swift: app panes always hosted (`--host [-c id]`), with native
   startup/error and host-stopped cards replacing the journal fallback.
   Needs the two owner decisions above.
6. Swift: one surface. `makePane` runs the editor-only client as the pane
   surface laid out as the card; delete the dock handoff, `presizeHidden`'s
   dock branch and the spike hooks; repoint Peek; update the `ui-loop` skill.
   The Python handoff code stays for the standalone fork until that drops it.
7. Not part of 3a: removing the dead seam hooks in the fork (and
   `make app-lock`). Raise it with the owner once 3a is done; it depends on
   3b and step 4.

### 3b. Native composer (undecided)

Replace the prompt_toolkit editor with a Swift one, sending `submit`,
`command`, `cancel` and the other intents over the two-way client from 3a.
Only worth doing if the goal becomes no terminal surfaces at all.

The long tail is the risk: 14 `.popup()` call sites and roughly 6 to 7k lines
of `*_ui.py` (config, tree, sessions, inspector, edit review, jobs, workers,
sign-in). As long as one of them falls back to a terminal overlay, the app
still needs a terminal emulator.

Decide after 3a, when the editor is a leaf and the cost of keeping it is clear.

### 4. Swap the container (only if 3b is done)

Once nothing draws through a terminal, the app gets its own Xcode project. The
APC protocol, `dock.py` and the popup bridge go away, and so do the Zig
toolchain, `app/ghostty.lock` and the copy-into-the-fork build.

Things the fork provides today that would need rebuilding:

- Theme and fonts. The transcript theme reads `ghostty.config` colors.
- The debug socket, about 1,400 lines of Swift that live in the fork and would
  move into `app/macos`.
- App basics: menu, keybindings and window restoration.

## The shell split: libghostty or something else

Only relevant if step 4 happens. "Keep only libghostty for the split" is less
clean than it sounds. From a quick search of third-party write-ups, not
upstream docs:

- `libghostty-vt` reached 1.0 in November 2026 and is stable, but it covers
  only VT parsing and terminal state. No renderer, fonts or pty.
- The full embedding API (`ghostty_surface_*`, which the app uses now) is what
  has the renderer. Upstream labels it internal and expects more breaking
  changes.

So "libghostty only" means keeping `GhosttyKit` and vendoring Ghostty's Swift
surface layer: the fork's app shell goes, but we still track unstable
internals. SwiftTerm (an ordinary Swift package with a pty-backed AppKit view)
is the lighter option for a plain shell split. Spike it before committing,
since rendering quality will be a step down from Ghostty.

## Open questions

- Do we want 3b at all? If so, how much editor parity does the native composer
  need (vi keys, history, image paste, drafts)? That sets its size more than
  anything else.
- Is SwiftTerm-level rendering acceptable for the shell split, or does it need
  to look like Ghostty?

## Related

- [ghostty-app-plan.md](ghostty-app-plan.md): the original plan for building
  the app on the fork.
- [desktop-host-plan.md](desktop-host-plan.md): the earlier host-client design,
  useful background for step 3a.
