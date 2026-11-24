# Reader mode plan

Status: all five phases built and reviewed, not yet judged by eye. Fork
(`ghostty-smooth`, branch `pcode-smooth`): `9c5ca8751`, `be9f810b9`,
`cf836a3f0`, `06e21e48a`, `243683afa`. pcode: `0432dc90`, `849fb2ed`. Builds on the terminal-side glide in
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
- `f`, `b`, `d`, `u` to page; `j`, `k` scroll steadily while held.
- `Space` to cruise at a steady reading speed and to pause, `l`/`h` (or the
  right and left arrows) to go faster and slower.

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

Additions to the `ESC _ smooth;... ESC \` family, as built. The query reply is
`smooth;ok;10`; pcode glides on 9 or 10 and only marks on 10.

- `smooth;mark;K`: tag the cursor's row, 1 turn (the submitted prompt's first
  row), 2 block (a code block or file-edit diff). Stored in a 2-bit
  `Row.smooth_mark`, so it moves with the row; reflow keeps it on the first
  line only. pcode writes it just before the row's text, replay included.
- `smooth;park`: arms a park bound to the next turn mark. When that row is
  above the active area's top while the viewport follows the bottom, the
  renderer pins the viewport there (`State.parkIfDue`) and the surface enters
  the reader table. A keystroke or leaving the bottom drops it. pcode sends it
  just before the turn mark of a prompt from `begin_turn`, when `reader_park`
  is on.
- `smooth;park;off`: pcode sends it once the turn's rows are all written. The
  terminal fires the park first if those rows made it due, else drops it, so
  later output can't park at an old prompt.
- The park tracks its own row (a tracked pin) and drops itself if that row is
  evicted, cleared or unmarked. Trap: searching for the newest turn mark
  instead pinned at an older turn after pcode's resize replay cleared the
  screen.
- No `follow` verb: nothing needed it.

Actions bound in the table: the existing scroll actions (now glided), plus
`smooth_hold:±1`, `smooth_cruise`, `smooth_cruise_speed:±N`,
`smooth_jump_turn:±1`, `smooth_jump_block:±1`. `y` sends F20 (`csi:34~`),
which pcode binds to its response and snippet copy chooser.

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
  on to the next whole row, so a tap moves one), `f`/`b`, `d`/`u` (and
  Ctrl), `g`, `G`, the cruise keys (phase 2), and
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

The keys are a media player's, not a pager's (fork `b5b137905`, `ce7783bb3`):

- **`Space` is play and pause**, not page down (`f` and `d` still page).
  Pause is what a thumb does to a moving page, so `Space` must never speed
  up: a key that both starts and accelerates makes the reader overshoot
  the line they wanted to stop on. `c` still works.
- **Held from a start, `Space` boosts to 3x** until it is let go, then
  carries on at the kept speed. The boost comes from key repeat, so the
  press that pauses is instant (no tap-or-hold timer) and holding it never
  restarts the cruise.
- **`h`/`l` and the left/right arrows shift the gear** (`-` and `=`/`+`
  still do). Up and down move the page, so the free axis is the pace, and
  it stays on the home row. Faster while stopped starts the cruise; slower
  than the lowest gear (the last above 0.5 rows/s) stops it. A step past a
  limit is not taken rather than clamped, so the gears stay put. A held
  gear key shifts once.
- **`k` stops a cruise** and scrolls back up ("wait, go back"); `j` adds to
  it while held.
- Speed changes ease in over about 0.12 s, and a pause carries the speed on
  to the next whole row instead of snapping back to the nearest.
- The speed starts at `smooth-cruise-speed` (Ghostty config) and a new tab
  or split inherits the last speed used. Not kept across a restart: set
  the config for that.
- Each cruise key shows the state for a moment in the native toast
  (`▶ 2.5 rows/s`, `▶▶ 7.5 rows/s`, `⏸ 2.5 rows/s`): the core sends itself
  a `pcode;toast`, so there is no new Swift. macOS only.

### 3. Marks and jumps: built

- `[`/`]` for turns (`[` lands on the current request first; past the newest
  turn is the bottom), `{`/`}` for blocks. Jumps count from where a glide is
  headed, so repeated presses step on.
- pcode finds marked rows with a zero-width sentinel (U+2061..U+2064) that
  `TaskPrompt`, pcode's `Markdown` code blocks and `EditTranscript` emit only
  inside `TerminalOutput._render` (`row_marks.py`); `_enqueue` strips it
  into `QueuedRow.mark` before anything reads the row. Not a Rich control
  segment: the render console writes to a `StringIO` without
  `force_terminal`, so Rich drops control text.
- Marks need the startup probe, which asks in Ghostty whatever
  `smooth_scroll` and `reader_park` say. Sentinels are only rendered when the
  terminal takes marks, and never inside thinking: its blank-row folding kept
  a sentinel's padding row (trap: an extra row for every user).

### 4. Park: built

- `reader_park` (default off: a park used to enter the reader table unasked,
  so the next typed letter ran a reader key). Steering and shell-mode prompts are marked but
  never park.

### 5. Copy: built

- `y` leaves the table, returns to the bottom and opens pcode's copy chooser
  for the last turn's response and snippets (from message history, not
  terminal cells).

## To check in a spike

- **Feel.** Spring stiffness (160), the 96-row jump cap, hold speed (12 rows/s)
  and the default cruise speed (2 rows/s) are guesses; tune by eye.
- **Park and pcode's conveyor.** Parking leaves the active area, where
  pcode's region glide stops drawing, so a batch mid-glide pops into place.
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
- **Resize while reading back: built, not yet seen in a running app.**
  pcode's resize replay clears scrollback (`\x1b[H\x1b[2J\x1b[3J`) and
  rewrites it, which used to leave the viewport at the bottom. pcode now
  writes `smooth;replay;begin` before the clear and `smooth;replay;end`
  after the rows, whenever it writes marks. At `begin` a viewport in
  history becomes an anchor (`ViewportGlide.Anchor`): how many marks of
  any kind lie below its top row, and how far that row is between the
  mark above and the mark below, as a fraction. At `end` the terminal
  finds the same two marks and scrolls to the same fraction between them.
  - A fraction, not a row offset: the rows wrap differently the second
    time, so a section of 40 rows may come back as 25.
  - Counted from the bottom, so scrollback evicted off the top does not
    shift it.
  - Fewer marks afterwards than were below the reader: the viewport is
    left where the clear put it. A place now inside the active area is
    the bottom.
  - No protocol version for it. pcode takes a query reply only up to the
    version it knows, so a bump would turn gliding off in every pcode not
    yet updated. A fork build without the verbs logs a warning and
    ignores them.
  - To check by eye: that nothing moves the viewport between the window
    resizing and pcode's `begin` (Ghostty's own reflow keeps a pinned
    viewport), and how it looks through a drag, which replays many times.
- **Full-screen popups.** They redraw the conversation afterwards, so the
  reading position is lost. Overlay popups, the normal case in Ghostty
  Smooth, are not affected. Accepted for now.
- **`y` while the reader table is active.** The table swallows keys, so `y`
  has to be passed through to pcode or bound to send it.
- **Marks through a replay.** The replay path must emit the same marks as
  live output, or jumps break after the first resize.
