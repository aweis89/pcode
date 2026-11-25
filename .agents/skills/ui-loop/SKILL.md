---
name: ui-loop
description: See and drive the Pcode macOS app (the Ghostty fork, including the Swift workspace spike) while changing its UI. Use when working on the app's Swift code, popups, drawer, docked editor or any pcode rendering inside the app, to snapshot windows, send keys and clicks, check selection and focus, and catch one-frame flicker yourself instead of asking the user to look.
---

# UI loop

The Pcode app ([aweis89/ghostty-smooth](https://github.com/aweis89/ghostty-smooth),
branch `pcode-smooth` and the spike branches off it) has a debug socket. Start
the app with `GHOSTTY_DEBUG_SOCKET=<path>` and `scripts/gdbg.py` can then read
and drive it:
- **What it gives you:** every terminal's frame, grid size, focus and text, plus
  view and layer trees, hit-testing, synthetic keys, clicks and drags,
  window snapshots and frame recordings.
- **No macOS permissions needed:** everything runs inside the app, so it works
  without Screen Recording or Accessibility. `screencapture` and `osascript`
  keystrokes fail in agent shells.
- **When it's off:** with the variable unset, the socket costs nothing.

Before reporting a UI change as done, check the result yourself: snapshot it and
look at it with `read_file`, then drive the interaction that broke.

## Build, launch, look

```sh
# In the Ghostty checkout. Use a separate bundle so the user's Pcode.app is untouched.
# It prints "installed builds/<APP_NAME>-<rev>.app": launch that path.
APP_NAME=pcode-mytest BUNDLE_ID=com.pcode.terminal.mytest scripts/install-macos.sh --build

# From the pcode repo. Replaces whatever app was on the socket, waits for a terminal.
G="python3 -I scripts/gdbg.py"
$G launch --app <ghostty-checkout>/builds/pcode-mytest-<rev>.app --command /tmp/run.sh
$G dump                      # windows, surfaces (id, overlay or pane, grid, frame, focus)
$G snap /tmp/s.png           # then read_file /tmp/s.png
```

Don't launch `builds/current`: every install repoints it, whatever the
`APP_NAME`, so in a checkout other sessions build in it is whoever built last.

`/tmp/run.sh` is whatever the first terminal should run, for example
`cd /tmp/demo && exec pcode --no-worktree`, or
`PCODE_DOCK=1 uv run --no-sync pcode` in a pcode worktree for the spike's dock.
Launching brings the window to the front once. After that, nothing in the
harness takes focus from the user unless you run `activate`.

## Commands

| Goal | Command |
| --- | --- |
| Terminal text (what the user sees) | `text -s ID` (`--scope screen` for scrollback, `selection`) |
| Wait for a state instead of sleeping | `wait 'regex' -s ID --timeout 10` (`--absent` to wait for it to go) |
| Keys | `key ctrl+a backslash enter`, `key cmd+shift+]`, `type 'some text'` |
| Click / select | `click --cell COL ROW -s ID`, `drag --from-cell 2 3 --to-cell 30 3 -s ID` (replies with the selection) |
| Which view takes a click | `hit X Y` (window points from the top-left) |
| Clips, corner radii, shadows, colors | `layers` (SwiftUI clips via layers, so `views` won't show them) |
| Picture | `snap [path]`, `--crop -s ID`, `--rect X Y W H`, `--retina`, `--raw -s ID` (the terminal's whole grid, unclipped) |
| Flicker | `record --seconds 2` then read the flagged `flash` frames from its directory |

Surface IDs are the 8-character prefixes `dump` prints, and they change on every
launch. `--json` prints the raw reply.

## Traps

- **A covered window stops drawing.** Ghostty skips rendering a fully covered
  window, so only `snap` and `record` show it as it is now: they wake its
  terminals first. `dump` marks such a window `covered(not drawing)`. `text`
  reads terminal state, so it is always current.
- **Background windows lose the first click.** AppKit spends the first click on
  a background window activating it, so a real click there never reaches the
  terminal. The harness delivers clicks straight to the view under the pointer
  instead, which keeps focus with the user. Use `activate` when a test needs
  the real first-click path.
- **Snapshots are composited inside the app.** Window-server effects are
  missing: blur, vibrancy, backdrop layers and the window's own shadow. For
  those, the user has to look, or grant Screen Recording to the app you run in
  and use `screencapture -l <window number from dump>`.
- **`record` samples; it can miss frames.** It captures when a terminal
  presents a frame and on a timer (for SwiftUI animation and terminals that
  appear mid-recording), but each capture renders the whole window on the main
  thread, so presents that land during a capture are merged or lost, and the
  capturing slows the app it measures. A flagged flash is real; no flag is not
  proof of no flash. It stops at 900 frames (`hit_frame_cap` in the reply).
- **Synthetic mouse events skip AppKit tracking loops.** Terminals don't run
  one, so clicks and drags on them are faithful. A control that does (buttons,
  split dividers, scrollers, a right-click menu) may stay pressed or open; check
  with `snap`, and press `escape` or click elsewhere to clear it.
- **Keys assume a US layout.** Key codes are US ANSI; characters without a key
  are inserted as text.
- **The Ghostty build has its own traps.** A release tag on `HEAD` fails the
  build ("tagged releases must be in vX.Y.Z format"), so commit first. A
  one-off `xcodebuild -create-xcframework` failure (exit 70) passes on rerun.
  Compile errors are hidden behind zig's "exit 65". To see them, run
  `xcodebuild -target Ghostty -configuration ReleaseLocal -arch arm64 build SYMROOT=/tmp/x | grep error:`
  in `macos/`.
- **The workspace window ignores `--command`.** With workspace mode on (the
  default), the window resumes a session by running `$PCODE_COMMAND`, and
  `launch` passes your environment on. Run from inside a pcode session, that
  is the parent's own pcode, not your worktree's: check with
  `pgrep -fl bin/pcode`. Export `PCODE_COMMAND=<worktree>/.venv/bin/pcode`
  (plus `PCODE_DOCK=1` for the dock) before `launch`.
- **Shared worktrees collide.** Another session may be building and
  relaunching in the same Ghostty worktree. Commit only your own paths
  (`git commit <paths>`, never `-a`), and use your own `APP_NAME`, bundle ID
  and socket path (`--socket`) so your relaunches don't kill its test runs.
