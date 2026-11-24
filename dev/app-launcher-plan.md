# Pcode.app launcher and surface defaults plan

Status: design settled (2026-11-23), not yet implemented. Builds on
[ghostty-app-plan.md](ghostty-app-plan.md) and the existing CLI picker in
`src/pcode/launch.py`.

## Problem

In a dedicated Pcode terminal app, dropping every new window and tab into `~`
with a plain shell forces users to `cd` and run `pcode` every time. On the
other hand, overriding Ghostty's `command` with a picker spawns a PTY and
wrapper process just to ask a question, collides with users' own shell or
`command` config, and gets in the way when all you want is a companion shell
split beside an agent.

## Core model

Match the default surface to what the user is trying to do:

- New windows and tabs usually mean starting or switching to a project, so they
  open a native Swift launcher.
- Splits usually mean a helper pane in the same directory (tests, `git`, a dev
  server, or a second agent), so they inherit the active pane's working
  directory and open a shell by default.

Each surface action accepts one of three values:

- `launcher`: open the native workspace and session picker.
- `pcode`: skip the picker and start a fresh `pcode` session directly in the
  active pane's working directory. Falls back to `launcher` when there is no
  active project (for example, when `cwd` is `~` or no pane is focused).
- `shell`: open a plain Ghostty `$SHELL` surface in the active pane's working
  directory, with no wrapper process.

## Config and keybindings

Ghostty avoids "horizontal" and "vertical" split names because devs disagree on
whether they describe the divider or the layout. Use Ghostty's own directional
names (`right` and `down`) in `~/.config/pcode/terminal.conf`:

```ini
default-new-window   = launcher   # app launch, Cmd+N
default-new-tab      = launcher   # Cmd+T
default-split-right  = shell      # Cmd+D (side-by-side)
default-split-down   = shell      # Cmd+Shift+D (top/bottom)
```

Splitting right and down into separate keys lets people who like an agent
side-by-side with a bottom shell flip one line (`default-split-right = pcode`,
`default-split-down = shell`), while people who want Pcode.app to behave like
stock Ghostty can set `default-new-tab = shell`.

Bare Ghostty actions resolve through those four defaults, and explicit action
arguments let users bind any combination directly:

- `new_window`, `new_window:launcher`, `new_window:pcode`, `new_window:shell`
- `new_tab`, `new_tab:launcher`, `new_tab:pcode`, `new_tab:shell`
- `new_split:right`, `new_split:right,pcode`, `new_split:right,shell`, `new_split:right,launcher`
- `new_split:down`, `new_split:down,pcode`, `new_split:down,shell`, `new_split:down,launcher`

Ship sensible secondary defaults for the opposite mode (for example,
`Cmd+Opt+T` for `new_tab:shell` and `Cmd+Opt+D` for `new_split:right,pcode`)
so both workflows stay one keystroke away without editing config. If a native
Preferences UI is added later, the four config keys map directly to four
dropdowns: New Window, New Tab, Split Right (Side-by-Side), and Split Down
(Top/Bottom).

## Architecture: pre-PTY Swift view + Python execution layer

Keep the Swift layer thin so the Ghostty fork stays easy to rebase, and keep
session/environment logic in Python where it is already tested.

### Native launcher surface (Swift)

When an action resolves to `launcher`, the tab or split mounts a SwiftUI
launcher view instead of spawning a PTY. It uses the active Ghostty theme's
background, foreground, accent, and font so it feels like part of the terminal.

Once the user makes a choice, the launcher swaps itself out for a real Ghostty
`SurfaceView`:

- For `shell`: spawn a completely vanilla `SurfaceView` with
  `workingDirectory = chosenPath` and the user's normal `$SHELL`. Ghostty's
  shell integration, OSC 7 `cwd` tracking, and prompt jumping work untouched.
- For `pcode` (new, continue, or attach): spawn a `SurfaceView` with
  `workingDirectory = chosenPath` and a non-interactive `pcode` launch command
  that handles `direnv` and the post-exit login shell fallback.

If `pcode` is not found on the login `PATH`, the launcher shows install
instructions instead of failing silently or closing the pane.

### Data and execution boundary (Python)

Do not reimplement session scanning, worktree folding, temporary-root
filtering, or `direnv` handling in Swift. Reuse `src/pcode/launch.py`:

- Workspace listing: expose a fast, machine-readable workspace list (or a
  lightweight cached index maintained by `pcode` and updated when sessions or
  hosts change) matching `launch.workspaces()` so the Swift picker renders in
  under a frame without waiting on a cold Python import.
- Non-interactive exec: add a non-interactive entrypoint (or flags on
  `pcode --launch`) that accepts a target path plus action (`new`,
  `continue <id>`, `attach <id>`) and reuses `launch.command()` for
  `direnv exec`, `PWD` cleanup, and the post-exit login shell wrapper so the
  pane stays open as a shell when `pcode` quits.
- Keep `pcode --launch` / `pcode --launch shell` working unchanged for people
  running `pcode` in stock Ghostty or other terminals.

## Launcher UX

```text
  > filter projects or type a path...

  › ~/p/pcode           ● 1 running · 4 sessions · 2m     [Enter]
    ~/p/ghostty-smooth    2 sessions · 3h
    ~/p/dotfiles          1 session · 2d

  ↩ new pcode   ⌘↩ continue/attach   ⇧↩ shell here   o open folder   esc shell
```

Expected behaviors:

- Typing immediately fuzzy-filters the project list. Typing a raw path (such as
  `/tmp` or `~/p/new-repo`) works even when the directory has no prior
  sessions.
- Projects with active session hosts show a live indicator (`● N running`), and
  `Cmd+Return` attaches to a running host or continues the latest session.
- `Return` starts a new `pcode` session in the selected directory; `Shift+Return`
  (or `s` when the filter box is empty) opens a plain shell there.
- `Esc` dismisses the launcher into a plain shell in the inherited `cwd` (or
  `~` for a fresh window/tab).
- `Cmd+O` (or `o` when the filter box is empty) opens a native `NSOpenPanel`
  folder chooser.
- Dragging a folder onto the Dock icon or onto the launcher surface skips the
  picker and starts `pcode` in that folder.

## Rollout phases

1. **CLI groundwork (`pcode` repo):**
   - Factor a non-interactive exec path out of `src/pcode/launch.py` so an
     external launcher can invoke `pcode` in a chosen directory with `direnv`
     and post-exit shell fallback.
   - Add a fast workspace/host metadata feed (either a cached JSON file under
     pcode's state dir or a fast read path Swift can parse directly without
     spawning Python on every `Cmd+T`).
   - Make sure `pcode` emits OSC 7 with its workspace directory so Ghostty
     splits created from a `pcode` pane inherit the right `cwd` (verify whether
     this already happens).
2. **Fork implementation (`ghostty-smooth` / `Pcode.app`):**
   - Add the four `default-*` config keys and explicit `new_window`, `new_tab`,
     and `new_split` action arguments.
   - Build the pre-PTY SwiftUI launcher view and swap-to-`SurfaceView` handoff.
   - Wire `NSOpenPanel`, Dock folder open, drag-and-drop, and missing-`pcode`
     fallback.
3. **Polish:**
   - Live refresh of running host badges while the launcher is open.
   - Global quick-switcher (`Cmd+K`) to jump to an existing `pcode` tab/session
     or open a new one from any pane.
   - Native Preferences dropdowns for the four surface defaults.

## Open questions to check during implementation

- **OSC 7 from `pcode`:** When splitting a pane that is currently running
  `pcode` (not a shell), Ghostty needs the pane's current working directory.
  Check whether `pcode` already emits OSC 7 on startup/directory change; if
  not, emit it so `default-split-* = shell` and `default-split-* = pcode`
  reliably inherit the workspace directory.
- **Cold startup latency for the workspace list:** Spawning Python on every
  `Cmd+T` adds ~100-200 ms. Having `pcode` write a small JSON snapshot of
  recent workspaces (or having Swift read the session/host state files directly
  with a small shared schema) keeps `Cmd+T` instant.
- **Login `PATH` resolution from Finder:** A Finder-launched app does not
  inherit interactive shell `PATH`. Reuse the login-shell resolution already
  tested for `pcode --launch shell` (zsh, bash, fish) when checking for `pcode`
  and invoking it.
