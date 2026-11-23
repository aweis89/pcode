# Reader mode plan

Status: phases 1 and 2 built in the fork (`ghostty-smooth` commits `9c5ca8751`,
`be9f810b9` and `cf836a3f0`, branch `pcode-smooth`); first feedback applied. Builds on the terminal-side glide in
[ghostty-app-plan.md](ghostty-app-plan.md) (protocol 9, `smooth_scroll.py`).

## Goal

Read a long response without chasing it. A new turn can stop after its first
page instead of scrolling away, and vim-style keys move through the
conversation with the same 120 Hz glide that streamed output has:

- `g` to the top of the chat, `G` back to the live bottom.
- `[` to the start of the current request (again: the turn before), `]` to
  the next one. The turn mark sits on the prompt line, so the request and the
  response under it are one jump.
- `{` and `}` between code blocks and diffs.
- `Space`, `d`, `u` to page; `j`, `k` scroll steadily while held.
- `c` to cruise at a steady reading speed, `=`/`+` and `-` to change it.

All single keys: as a sequence (`gg`, `[[`), the first key followed by any
other types both into pcode.

## Decisions

- **The whole viewport scrolls, not a region.** Regions draw nowhere once the
  viewport leaves the bottom (`generic.zig`, "Scrolled into history"), and only
  128 rows of scrollback are ever built above one. Scrolling a region through
  history would mean rebuilding selection, links, search and mouse mapping for
  it. Ghostty's own scrollback already has all of those.
- **The editor and status line scroll away while reading back.** That is the
  cost of the line above. Typing snaps to the bottom (`scroll-to-bottom`
  defaults to `keystroke`), which is the exit gesture.
- **The terminal owns the keys.** A `reader` key table (`activate_key_table`)
  binds them to scroll actions. A key the table consumes never reaches the
  pty, so it cannot trigger the keystroke snap, and pcode's event loop is not
  in the path between a key and the motion.
- **pcode holds no rows.** Parking is the terminal not following output, not
  pcode withholding it. Output keeps landing in scrollback, so search and
  selection reach it, and `flush(drain=True)` has nothing extra to dump.
- **Landmarks are marks the terminal keeps, not row offsets pcode tracks.** A
  mark is attached to a row, so reflow and scrollback eviction are the
  terminal's problem and it already solves them for its own pins.
- **A private mark verb, not OSC 133.** OSC 133 means "shell prompt" to
  Ghostty and may bring prompt handling with it (clearing prompt rows on
  resize is the one to check). One private verb also covers code blocks and
  diffs.
- **No `rate`, `pause` or `snap` verbs.** They controlled held rows, and
  nothing is held. The protocol 9 conveyor is unchanged and still only runs at
  the live bottom.
- **Indicators are out of scope here.** OSC 9;4 is already the turn progress
  bar (`terminal_notify.py`), so a reading indicator cannot simply reuse it.

## Split

| Side | Owns |
|---|---|
| ghostty-smooth | Glided viewport scroll, the `reader` key table, cruise and its speed, storing marks and jumping between them, parking at a mark |
| pcode | Emitting marks as it writes turn starts, code blocks and diffs; asking for a park at the start of a turn; `y` to copy from message history |

pcode tracks no reader state and handles no reader keys.

## Protocol 10

Additions to the `ESC _ smooth;... ESC \` family. Names are provisional.

- `smooth;mark;KIND`: tag the cursor's row. Kinds: turn, code, diff.
- `smooth;park;KIND`: when the newest mark of that kind reaches the top of the
  screen, pin the viewport there and stop following output. The terminal
  picks the moment, since only it knows the viewport.
- `smooth;follow`: scroll to the bottom and leave the reader table. For pcode
  to call when it needs the user at the editor.

Everything else is Ghostty actions bound in the key table, with no escape
sequence: the existing `scroll_page_fractional`, `scroll_page_lines` and
scroll to top or bottom, plus new `jump_to_mark:KIND,±1`, `cruise` and
`cruise_speed:±1`.

## Phases

### 1. Glided scroll and the key table (fork only): built

- The scroll actions (page, half page, lines, top, bottom) glide.
  `ViewportGlide.zig` keeps a visual position, moves the viewport in whole
  rows and draws the remainder through the sub-row offset trackpad scrolling
  already uses, so no overscan or cell-building changes were needed. Jumps
  longer than 96 rows jump the excess and glide the rest.
- A trackpad scroll, the keystroke snap and `scroll-to-bottom = output` cancel
  a glide. So does any other viewport move (a clear, `jump_to_prompt`, the
  alternate screen): the glide remembers where it left the viewport and stops
  if that changed, rather than gliding on from the new place.
- Default `reader` table, entered with Cmd+R (Ctrl+Shift+R off macOS): `j`/`k`,
  arrows (held: `smooth_hold`, a steady 12 rows/s from press to release, then
  on to the next whole row, so a tap moves one), `Space`/`f`/`b`, `d`/`u` (and
  Ctrl), `g`, `G`, `c`, `=`/`+`/`-`, and
  `q`/`Esc`/`i` to leave and go back to the bottom. Shortcuts held with Ctrl,
  Alt or Cmd skip the table's catch_all and reach their usual bindings. Any other
  key leaves the table and still reaches pcode (`unconsumed:catch_all`), which
  snaps to the bottom.
- `g`, not `gg`: as a key sequence, `g` followed by anything else types both
  keys into pcode and leaves the table on.
- Trap found on the way: a table's `catch_all` matched a lone modifier press,
  so holding Shift to type `G` would have left the table first. `getEvent` now
  never matches `catch_all` for a modifier key.
- `g` goes to the top of scrollback, and stays that way.
- The spring is stiffness 160 (first tried 400, too fast).

### 2. Cruise (fork only): built

- `smooth_cruise` toggles a constant-speed scroll down (default 2 rows/s);
  `smooth_cruise_speed:±N` changes it by factors of 1.25, kept between cruises.
- Stops at the live bottom. Other scroll keys add to the motion while it runs.

### 3. Marks and jumps

- Fork: `mark`, `jump_to_mark`, protocol bumped to 10.
- pcode: emit marks in `_enqueue()` where rows are split, only when the
  terminal answered 10.
- Keys: `[`/`]` for turns (`[` lands on the current request first), `{`/`}`
  for code blocks and diffs.

### 4. Park

- Fork: `park` and `follow`.
- pcode: a setting that sends `park` when a turn starts.

### 5. Copy

- pcode: `y` copies the code block or response from message history, not
  from terminal cells.

## To check in a spike

- **Feel.** Spring stiffness (400), the 96-row jump cap and the default cruise
  speed are guesses; tune by eye.
- **Synchronized output.** The glide steps in the renderer's frame update,
  which is skipped during synchronized output. The step after one is capped at
  50 ms, so a long pcode frame pauses the glide rather than leaping it.
- **Lock keys.** Caps Lock is not a modifier to Ghostty, so it still trips the
  catch_all and leaves the table.
- **Cmd+R on the alternate screen** enters a table that has nothing to
  scroll and swallows its keys until `q`.

- **The seam at the bottom.** Cruising down is a viewport glide until the
  viewport becomes active again, where the region conveyor takes over. That
  handoff has to look continuous, including when a batch glide is in flight.
- **Resize while reading back.** pcode's resize replay clears scrollback
  (`\x1b[H\x1b[2J\x1b[3J`) and rewrites it. Marks are re-emitted but the
  viewport lands at the bottom. Either accept that or have the terminal
  remember which mark it was at and pin again.
- **Full-screen popups.** They redraw the conversation afterwards, so the
  reading position is lost. Overlay popups, the normal case in Ghostty
  Smooth, are not affected. Accepted for now.
- **`y` while the reader table is active.** The table swallows keys, so `y`
  has to be passed through to pcode or bound to send it.
- **Marks through a replay.** The replay path must emit the same marks as
  live output, or jumps break after the first resize.
