# Terminal app (Ghostty fork) plan

Status: proposed implementation plan. Nothing here is built. This is the
alternative to [desktop-host-plan.md](desktop-host-plan.md): instead of a second
frontend, wrap the existing terminal app in a patched terminal emulator.

What was checked: pcode's own code, and the READMEs of the forks named below.
No fork's source was read or built. Ghostty config options are quoted from
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

## Design sketch

To be confirmed or replaced by Phase 0.

- pcode declares one region: the viewport rows above the editor and live panel.
- When pcode commits rows, it says so in the same synchronized update: "this
  region advanced by N rows".
- The terminal draws the region offset down by N rows and springs the offset to
  zero on its own frame clock. Rows outside the region never move.
- Trackpad scrolling through scrollback is separate and already handled by the
  fork's whole-grid shift.

This is an extension of the tlpinney region sequence (it has regions and
overscan, but only trackpad-driven and app-set shifts), with the spring taken
from or modelled on the parkers fork.

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
- **The base fork is chosen after Phase 0**, not before.
- **Build from source.** Do not ship or depend on another fork's binaries.
- **pcode stays usable everywhere.** The region sequence is opt-in by
  capability query. `PCode.app` is one terminal pcode runs well in, not a
  requirement.
- **Thin app.** `PCode.app` runs the `pcode` on `PATH` and bundles no Python.
- **Two prefixes.** The terminal's prefix (splits, tabs) and pcode's
  `key_prefix` (actions) must differ, since the terminal sees keys first. Ship
  defaults that do not collide, and document how to change both.
- **Own name and icon.** The bundle uses no Ghostty branding. Keep the upstream
  licence notice.

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
rest, and a fast command log does not lag. Fail: stop, and record what was
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

### Phase 1: the patch, properly

- Settle the sequence: declaration, advance, clear, capability query, and a
  version number. Write it down in the fork the way `SMOOTH.md` does.
- Spring constants as terminal config, with a snap threshold for backlog.
- Tests in the fork for the region bookkeeping. Rendering is checked by eye and
  by a screen recording stepped frame by frame.
- Decide the rebase policy: track upstream releases, not `main`, and keep the
  patch as a short series of commits so each rebase is reviewable.

### Phase 2: pcode support

- Capability query at startup alongside the existing terminal queries
  (`theme.py` shows the rule: never query while prompt_toolkit owns input).
- Declare and advance the region inside the existing atomic handoff, so it
  lands in the same synchronized frame as the rows.
- Clear and redeclare around popups, external programs, and resize.
- A `smooth_scroll` preference (`auto`, `off`).
- Tests assert the bytes written. This touches the editor and streaming, so
  `make test-all` applies.

### Phase 3: app identity and config

- Bundle id, name, and icon, so `PCode.app` sits beside Ghostty with its own
  permissions.
- Skip the user's Ghostty config; load a bundled one, then an optional
  `~/.config/pcode/terminal.conf`.
- Bundled config: light and dark themes that follow the system, smooth-scroll
  defaults, and prefix key sequences for splits and tabs.
- Check whether the terminal theme and pcode's Pygments-derived chrome
  (`syntax_colors.py`) look right together, since pcode reads the terminal's
  background at startup.

### Phase 4: launch

- **Workspace.** A Finder launch starts in the home directory, and pcode wants
  a project. Options: a small picker of recent workspaces as the first screen,
  "Open with PCode" on folders, or a `pcode://` URL. Pick one.
- **Environment.** A login shell gives `PATH` but not direnv, whose hook fires
  at a prompt, not under `-c`. Launch as a login shell running
  `direnv exec <workspace> pcode` when direnv is installed. Test with fish,
  zsh, and bash.
- **What a split runs.** `command` (every pane is pcode) or `initial-command`
  (first pane is pcode, splits are shells). A shell beside the agent is the
  common want, so start from `initial-command` plus a keybinding that opens a
  new pcode pane, if Ghostty's keybind actions allow a per-split command.
- **Attach.** A pane for an existing session is `pcode --attach ID`; sessions
  already survive the window closing because the host owns them.
- **Missing pcode.** Show install instructions in the pane instead of closing.

### Phase 5: packaging

- Developer ID signing and notarization.
- `.dmg`, and a Homebrew cask that depends on the pcode formula.
- A release workflow that builds from a pinned fork commit.
- Linux only after the OpenGL renderer has the patch.

## Risks

- **Fork upkeep.** This is a Zig and Swift terminal emulator, in languages this
  repo does not use, rebased against a fast-moving upstream. We also own its
  security updates. A small patch series is the only thing that keeps this
  cheap.
- **The spring may not survive real output.** Row commits are not perfectly
  regular, and the editor handoff is frequent. Phase 0 exists for this.
- **Niche payoff.** Smooth output reaches only people who run `PCode.app`
  outside tmux. Everyone else gets nothing new.
- **Upstream may get there first.** Ghostty has an open request for sub-cell
  scrolling ([discussion 3206](https://github.com/ghostty-org/ghostty/discussions/3206)).
  If upstream ships pixel scrolling, the fork shrinks to the region sequence,
  which could then be proposed upstream.

## Alternatives

- **No fork, launcher only.** Phases 3 and 4 need no patched source: a launcher
  around stock Ghostty with its own config gives the app, splits, tabs, and
  themes. Worth shipping first if Phase 0 fails or drags.
- **Build on libghostty.** Ghostty's README describes `libghostty` as an
  embeddable library with a minimal example app. A small app of our own on top
  of it might be less to carry than a rebranded full Ghostty. The renderer
  patch is needed either way. Not investigated.
- **Electron client.** See [desktop-host-plan.md](desktop-host-plan.md). Pick
  that if rich blocks matter more than keeping one UI.
