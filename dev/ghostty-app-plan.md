# Terminal app (Ghostty fork) plan

Status: Phase 0 spike done (see results below); pcode support is next. This is
the alternative to [desktop-host-plan.md](desktop-host-plan.md): instead of a
second frontend, run the existing terminal app in a terminal that can shift
rows by pixels.

The spike changed the plan's shape. pcode can drive the glide itself through
the tlpinney fork's existing sequence, so the first deliverable is pcode
support for that sequence, not a patched or rebranded terminal. `PCode.app` is
deferred to an optional later phase. Ghostty config options are quoted from
memory and must be checked against the version we build.

## Goal

A standalone `PCode.app` that runs the real `pcode` TUI in a patched Ghostty, so
streamed output glides in by pixels instead of jumping by rows, with native
splits, tabs, and themes. No second UI to maintain, and the transcript stays in
real terminal scrollback as `PLAN.md` requires.

What this gives up compared with the Electron plan: rich blocks. Collapsible
tool calls and diff widgets are not possible on a cell grid.

## What pcode already does

- **Synchronized output.** Every scrollback write is an atomic handoff: erase
  the editor, write rows, repaint the editor, wrapped in mode 2026
  (`ui.py`: `SYNC_START`, `suspended_editor`).
- **Pacing.** `paced_scrollback` (`typed` by default) types prose onto a live
  row and commits each row when complete; code, tables, and tool output roll in
  a row per frame. Frames run at 30 a second.

So there is no pacing or sync work to add. The terminal receives a steady
stream of one-row scrolls, which is the best possible input for a spring.

## The central problem

pcode's editor and live panel are pinned to the bottom of the screen. When a
row is committed, the terminal sees the grid move up one row and the editor
repainted where it was. It cannot tell content that scrolled from chrome that
was redrawn in place.

A spring keyed on "scrollback advanced" would therefore slide the whole
viewport, and the editor would dip and glide back on every committed row.
Neovide avoids this only because Neovim reports which region scrolled.

The fix has to be: pcode tells the terminal which rows are transcript, and the
terminal animates only those. Everything else in this plan is routine by
comparison, so it is validated first.

## Prior art

| Fork | What it has | Fit |
| --- | --- | --- |
| [tlpinney/ghostty](https://github.com/tlpinney/ghostty) (`SMOOTH.md`) | Trackpad pixel scrolling, plus an APC sequence for apps to declare scroll regions with an overscan row, and an app-set sub-cell shift. Recent, small, already renames the bundle so it installs beside Ghostty. macOS Metal only. | Closest. Has the region concept we need. No spring for app-driven movement. Personal experiment with no users. |
| [parkers0405/ghostty-pixel-scroll](https://github.com/parkers0405/ghostty-pixel-scroll) | Critically damped scroll and cursor springs. Also a Neovim GUI renderer, collab cursors, panels, and shader effects. Linux builds exist. | Has the spring. Far more than a light patch, and may be stale; check its last commit. |
| [shexe/ghostty-pixel-scroll](https://github.com/shexe/ghostty-pixel-scroll) | Trackpad sub-cell scrolling on v1.3.1, about 2,500 lines. | Small, but no regions and no spring. |

The tlpinney document makes one claim that shapes the design: an app cannot
animate smoothly itself, because its clock is not the terminal's frame clock.
If true, pcode sending a per-frame shift at 30 fps will stutter, and the
animation must run inside the renderer.

The Phase 0 spike contradicted that claim at 120 fps (see results).

## Design

App-driven, using the stock tlpinney sequence (protocol 3):

- pcode declares one region: the viewport rows above the editor and live panel,
  with the row just above the editor as the hidden overscan row.
- The next line to commit is drawn into that hidden row. A spring on pcode's
  own clock moves the region's shift (`oy`) from 0 toward one row, sent every
  display frame by a small ticker that writes only the declaration.
- When the line is fully revealed, the normal atomic handoff commits it to
  scrollback, draws the following line into the hidden row, and resets `oy` to
  0, in one synchronized frame.
- Trackpad scrolling through scrollback stays with the terminal; pcode only
  enables mouse reporting in popups.

If the app-driven version hitches under a busy event loop, the fallback is a
small fork patch: pcode says "slide this region up one row" once per line and
the terminal springs it on its own frame clock (the parkers fork has a spring to
model it on). That removes the ticker from pcode.

Cases the design must cover:

- **Screen not yet full.** New rows move the cursor down without scrolling.
  Nothing to animate.
- **Editor height changes.** The region's bottom edge moves when the editor
  grows or the live panel appears. That is not a scroll and must not animate.
- **Fast output.** A long command log arrives faster than the spring settles.
  Above some backlog the offset must snap.
- **More than one row at once.** Overscan in the existing protocol is one row.
  Pacing makes one row per frame the common case; decide what happens
  otherwise (snap, or keep the previous frame's rows).
- **Popups, pickers, external programs, resize, alternate screen.** Clear the
  region before each and redeclare after.
- **Unknown terminals.** Query the capability first. Stock terminals ignore an
  unknown APC, but pcode should not send it unasked.
- **tmux and ssh.** tmux will not relay the sequence, so smooth output is off
  inside tmux. ssh passes it through.

## Decisions

- **macOS first.** The tlpinney smooth rendering exists only in the Metal
  renderer. Linux needs the same work in the OpenGL renderer and is a later
  phase, not a launch target. Windows is out; the core is POSIX-only.
- **Base fork: tlpinney/ghostty.** Its region sequence was enough for the
  spike. Tell its author there is a second consumer and ask how stable the
  sequence is, since pcode will depend on it.
- **Build from source before shipping anything.** Do not ship another fork's
  binaries. For pcode support alone, users bring their own terminal, so this
  only applies once there is an app or a patch.
- **pcode stays usable everywhere.** The region sequence is opt-in by
  capability query. `PCode.app` is one terminal pcode runs well in, not a
  requirement.
- **Thin app.** `PCode.app` runs the `pcode` on `PATH` and bundles no Python.
- **Two prefixes.** The terminal's prefix (splits, tabs) and pcode's
  `key_prefix` (actions) must differ, since the terminal sees keys first. Ship
  defaults that do not collide, and document how to change both.
- **Own name and icon.** The bundle uses no Ghostty branding. Keep the upstream
  licence notice.
- **Gate on the probe, not a mode (2026-11-21).** One setting,
  `smooth_scroll` (`off` while experimental, `auto`), plus the startup query.
  There is deliberately no `cli`/`app` mode and no capabilities object yet:
  - A launch marker such as `PCODE_HOST=app` leaks. The environment is
    inherited by the session host the client spawns, by any `pcode` a shell
    tool runs, and by a tmux started inside the app. The probe answers "can
    this terminal glide" for exactly the process drawing on it.
  - It would lock out the patched terminal used standalone, which is the only
    setup that exists today.
  - "Host" already means session host (`host.py`, `PCODE_HOST_DIR`,
    `list_hosts`), and `output.app` is the prompt_toolkit `Application`. Say
    "terminal" for this layer, and `PCODE_FRONTEND` if a launch marker is
    ever needed.
  - Webview features would not be terminal capabilities. A GUI attaches over
    the session host protocol, which has its own `hello` handshake. Add a
    capabilities type when a second terminal-level feature exists, and make
    any future mode a preset of per-feature defaults, not a gate.
  - Capabilities are per client process, never per session: one session can be
    attached from the app now and from tmux later.

## Phases

### Phase 0: does it feel right (spike, no packaging)

1. Build the tlpinney fork from source. Run pcode in it unchanged and note what
   already works (trackpad scrollback) and what the editor does.
2. Behind an environment variable, have pcode declare the transcript region and
   use the existing app-set shift to animate a committed row at its own 30 fps.
   This costs little and tests the "apps cannot animate" claim directly.
3. If step 2 stutters, as expected, add the terminal-side spring for region
   advances. Hardcode constants; no config, no polish.
4. Replay a long recorded session through the real editor
   (`pcode-benchmark`'s paced replay) in both `typed` and `rows` modes.

Pass: output glides, the editor and live panel never move, text is sharp at
rest, and a fast command log does not lag. Measured as well as judged by eye:
log the gap between shift writes during a replay, and require 99% of gaps under
12 ms at 120 fps. Fail: stop, and record what was
learned here. The remaining phases are a rebranded terminal with a config file
and are not worth doing on their own.

#### Phase 0 results so far (2026-11-20)

- Ran tlpinney's signed prebuilt release (`smooth-v2026.10.31.2`), not a source
  build.
- [smooth_scroll_spike.py](smooth_scroll_spike.py) copies pcode's layout: a
  transcript region above a fake editor pinned to the bottom, with rows
  arriving in synchronized frames. The transcript is declared as a region with
  an overscan row. A closed-form critically damped spring runs on the app's
  own clock at 120 fps, and the fractional part goes out each frame as the
  protocol 3 shift (`oy`).
- Result, judged by eye: the spring glides pixel by pixel and plain row
  scrolling visibly jumps. The fake editor below the region did not move. The
  claim that apps cannot animate smoothly does not hold at 120 fps, as long as
  the shift is sent every display frame.
- Consequence: steps 1 and 3 may not be needed, and with them the Zig patch
  and the source build. The stock region sequence may be enough.
- Open: at first, spring mode showed misaligned characters at line ends. The
  likely cause was long test lines wrapping into the hidden overscan row; they
  are now cut to fit, but this is not confirmed. Real arrival rates are slow
  (a few rows a second for prose), so test at those rates.
- The fork also builds from source (`ed6a86659`, Xcode 27.2, Zig 0.17.1) with
  the commands in its `SMOOTH.md`. Trap: Xcode 26 and later ship without the
  Metal compiler. `zig build` then fails at `metal Ghostty (Ghostty.ir)`, and
  the follow-on `xcodebuild` error (no `GhosttyKit.xcframework`) hides the
  cause. Fix it with `xcodebuild -downloadComponent MetalToolchain` (about
  840 MB, no sudo needed).

#### App-driven glide in real pcode (2026-11-20): failed

Wired into pcode (branch `feat/smooth-scroll`, first commit) with a 120 Hz
asyncio ticker driving `oy` and a hidden row above the live panel. It caught
and shook. The per-tick log showed why:

- Ticks were late: 3,587 of 4,276 gaps were over 16 ms, against an 8 ms target.
  pcode's event loop is busy rendering.
- Every commit stalled the glide: the handoff that writes the row and repaints
  the editor took 15 to 23 ms, once per line.
- The region's bottom edge moved 255 times mid-glide as the live panel grew
  and shrank, and each move jolted the shifted rows.

So the fork author's claim holds for a real app, and the terminal-side spring
(the fallback above) is needed.

#### Terminal-side glide (2026-11-20): built, being judged

- Fork patch on `~/p/ghostty-smooth` branch `pcode-glide`: protocol 4 adds
  `ESC _ smooth;advance;ID;N ESC \`. The region's content is drawn N rows lower
  (capped at 3) and a critically damped spring returns it to rest. The spring is
  snapshotted at each frame update and evaluated at draw time, so motion
  follows vsync. Glide survives re-declarations that move the region's edges.
  Unit tests in `State.zig`.
- pcode (branch, second commit): no ticker and no spring. It writes rows as
  usual (rows pacing, typing off), sends `advance` in the same synchronized
  frame when the editor is pinned, and re-declares the region after each
  render so its bottom stays on the reserved, empty typing row. The shift form
  is no longer used, so no column is hidden.
- Known gap: rows drawn lower leave a band at the top of the screen with
  nothing in it, up to the glide distance, because the renderer has no row
  above the viewport to fill it with.

The original notes on the app-driven wiring follow.

What wiring it into pcode involves:

- **A display-rate ticker.** pcode's flush loop runs at 30 fps. The shift
  needs its own loop at display rate, and each tick is one small escape
  sequence, not a repaint.
- **The overscan row.** The row being revealed has to sit, hidden, in the row
  just above the editor before the shift brings it into view. It belongs to
  the live layout, not scrollback. After it is fully revealed, the normal
  handoff commits it to scrollback and resets `oy` to 0 in the same
  synchronized frame. That costs one blank row above the editor.
- **Typed prose.** Typing shows a partial row in the live area. Either the
  typed row is the one revealed, or typing is replaced by row reveals when
  smooth scrolling is on.
- **Screen not yet full, resize, popups, the alternate screen.** As listed
  under the design sketch.

Before wiring pcode, two checks in the spike:

- **Busy event loop.** The spike's `s` key injects random 10–60 ms stalls,
  standing in for Markdown rendering and tool events. If glides visibly hitch
  at `light`, go to the fork-patch fallback before touching `ui.py`.
- **Right column.** The shift form makes the rightmost column overscan too.
  Check whether a vertical-only shift (`ox` 0) still hides it, and whether that
  caused the misaligned line ends. If it does, pcode renders one column
  narrower, or a few-line fork patch keeps the column.

### Phase 1: pcode support (experiment)

In a worktree, behind the `smooth_scroll` setting (first `PCODE_SMOOTH_SCROLL=1`):

- Capability query at startup alongside the existing terminal queries
  (`theme.py` shows the rule: never query while prompt_toolkit owns input).
  Off inside tmux.
- The hidden row, the ticker, and commit-at-end-of-slide, as in the design.
  The ticker shares prompt_toolkit's output so writes never interleave.
- Typed prose reveals whole rows in smooth mode; typing into a revealed row can
  come later if it is missed.
- Clear and redeclare around popups, external programs, and resize.
- Replay a long session through `pcode-benchmark` and log shift-write gaps.

Then, if it holds up: a `smooth_scroll` preference (`auto`, `off`), tests that
assert the bytes written, `make test-all` (this touches the editor and
streaming), a review, and a docs page saying which terminal to run.

#### How the probe works (branch `feat/smooth-scroll`)

- `ESC _ smooth;? ESC \` goes in the same write as the OSC 11 background
  query, before it. A terminal answers in order, so once the background reply
  is in, a missing glide reply is final: no extra wait or timeout.
- It is only sent when `smooth_scroll` is `auto`, to the editor's own terminal
  (not the `/dev/tty` fallback `--print` uses), when the environment says
  Ghostty (`TERM=xterm-ghostty`, which survives ssh, or
  `GHOSTTY_RESOURCES_DIR`), and never under tmux.
- The version must match exactly (`PROTOCOL`, now 9: batch conveyor glides
  backed by scrollback and queued sweep/curtain reveals), because pcode
  mirrors the terminal's batch glide reach and conveyor speeds to decide when
  to hand off the next batch. Older protocols are not supported.
- A reply that lands after the probe stops waiting (slow ssh) is filtered out
  of typed input by `startup_input`, like a late background reply.
- Popups clear the regions (`TerminalOutput.release_screen`); full-screen
  views clear them by switching to the alternate screen.

### Phase 2: fork patch, only if needed

Only for problems Phase 1 cannot solve in pcode: hitches under load (move the
spring into the terminal), the hidden right column, or more than one overscan
row. If it happens:

- Settle the added sequence and its capability version, documented the way
  `SMOOTH.md` is, and offer it to the fork's author.
- Track upstream releases, not `main`, and keep the patch as a short series of
  commits so each rebase is reviewable.
- Build from source; the Metal Toolchain trap in the results applies.

### Phase 3: `PCode.app` (optional, deferred)

A product decision to make after Phase 1 ships, not the path to a first
result. It wraps everything below in one installable app.

#### App identity and config

- Bundle id, name, and icon, so `PCode.app` sits beside Ghostty with its own
  permissions.
- Skip the user's Ghostty config; load a bundled one, then an optional
  `~/.config/pcode/terminal.conf`.
- Bundled config: light and dark themes that follow the system, smooth-scroll
  defaults, and prefix key sequences for splits and tabs.
- Check whether the terminal theme and pcode's Pygments-derived chrome
  (`syntax_colors.py`) look right together, since pcode reads the terminal's
  background at startup.

#### Launch

- **Workspace (built, 2026-11-23).** A Finder launch starts in the home
  directory, and pcode wants a project. `pcode --launch shell`
  (`src/pcode/launch.py`) is the first screen: a picker of the projects with
  saved sessions that execs a fresh `pcode` in the chosen one, then a login
  shell there when it quits. It lives in the CLI, not the app, so the fork
  carries no Swift for it and it works in any terminal. "Open with PCode" on
  folders and a `pcode://` URL can come later; both only need to hand a path
  to the same code.
- **Environment.** A login shell gives `PATH` but not direnv, whose hook fires
  at a prompt, not under `-c`. `--launch` runs `direnv exec <workspace> pcode`
  when direnv is installed and an `.envrc` applies, or when `DIRENV_DIR` says
  the launching shell carries another directory's variables. A blocked
  `.envrc` makes that fail with direnv's own message rather than starting
  without the variables; with `shell` the login shell still follows. Still to
  test from the app with fish, zsh, and bash.
- **What a split runs.** `command` (every pane is pcode) or `initial-command`
  (first pane is pcode, splits are shells). A shell beside the agent is the
  common want, so start from `initial-command` plus a keybinding that opens a
  new pcode pane, if Ghostty's keybind actions allow a per-split command.
- **Attach.** A pane for an existing session is `pcode --attach ID`; sessions
  already survive the window closing because the host owns them.
- **Missing pcode.** Show install instructions in the pane instead of closing.

#### Packaging

- Developer ID signing and notarization.
- `.dmg`, and a Homebrew cask that depends on the pcode formula.
- A release workflow that builds from a pinned fork commit.
- Linux only after the OpenGL renderer has the patch.

## Risks

- **Depending on a one-person fork.** pcode support relies on an escape
  sequence from a personal experiment with no other users. Keep it opt-in by
  capability query so it costs nothing elsewhere.
- **Fork upkeep, if we patch.** A Zig and Swift terminal emulator, in languages
  this repo does not use, rebased against a fast-moving upstream. A small
  patch series is the only thing that keeps this cheap.
- **The spring may not survive real output.** pcode's event loop is busy, row
  commits are not perfectly regular, and the editor handoff is frequent. The
  stall mode and the Phase 1 replay exist for this.
- **Niche payoff.** Smooth output reaches only people who run the fork outside
  tmux. Everyone else gets nothing new.
- **Upstream may get there first.** Ghostty has an open request for sub-cell
  scrolling ([discussion 3206](https://github.com/ghostty-org/ghostty/discussions/3206)).
  If upstream ships pixel scrolling, the fork shrinks to the region sequence,
  which could then be proposed upstream.

## Alternatives

- **No fork, launcher only.** `PCode.app` itself needs no patched source: a launcher
  around stock Ghostty with its own config gives the app, splits, tabs, and
  themes. Worth shipping first if Phase 0 fails or drags.
- **Build on libghostty.** Ghostty's README describes `libghostty` as an
  embeddable library with a minimal example app. A small app of our own on top
  of it might be less to carry than a rebranded full Ghostty. The renderer
  patch is needed either way. Not investigated.
- **Electron client.** See [desktop-host-plan.md](desktop-host-plan.md). Pick
  that if rich blocks matter more than keeping one UI.
