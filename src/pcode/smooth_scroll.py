"""Experimental: glide new scrollback rows into view in a patched Ghostty Smooth.

The ``smooth_scroll`` setting turns it on (``auto``); see dev/ghostty-app-plan.md.
It needs the Ghostty Smooth fork, which answers the startup query
(`pcode.theme`) with a version from ``MIN_PROTOCOL`` to ``PROTOCOL``. Anything
else, including tmux, which does not relay the sequences, gets none of them.

The transcript rows above the editor and the typing row under them are
declared as one region, with one reserved blank row below as the region's
hidden overscan row. pcode hands each settled batch of rows to scrollback in
one synchronized frame and sends a batch ``advance`` for the rows it scrolled,
plus ``reveal`` animations for the rows it committed. The terminal paces the
rest on its own frame clock: a conveyor glide whose speed follows its own
backlog, a soft bottom-edge dissolve, and a queue of ink-sweep (prose) and
curtain (everything else) reveals, so pcode's busy event loop cannot make the
motion stutter.

Protocol 10 adds reader mode (dev/reader-mode-plan.md): pcode tags the first
row of each turn and of each code block or diff with ``mark`` so the
terminal's reader keys can jump between them, and ``PARK`` asks the terminal
to stop following output once a new turn's prompt scrolls off the top.
"""

import math
import os
import re

# The smooth-scroll protocol pcode speaks: 9 adds batch conveyor glides of up
# to MAX_BATCH_GLIDE rows backed by scrollback, soft bottom-edge dissolve, and
# queued multi-segment ink-sweep / curtain reveals with signed rows; 10 adds
# reader-mode row marks and park. The glide works from MIN_PROTOCOL up, since
# older fork builds still answer 9; marks need MARKS_PROTOCOL.
PROTOCOL = 10
MIN_PROTOCOL = 9
MARKS_PROTOCOL = 10
QUERY = b"\x1b_smooth;?\x1b\\"
# Any smooth-scroll reply, so a future shape is still kept out of typing; only
# `ok;N` with N in MIN_PROTOCOL..PROTOCOL turns the glide on.
REPLY = re.compile(rb"\x1b_smooth;([^\x1b\x07]{0,32})\x1b\\")
VERSION = re.compile(rb"ok;(\d{1,4})")

# Rows of batch glide the terminal keeps at most, and its conveyor's speed
# range (rows/s) and catch-up time, mirrored so pcode can estimate how much
# of the glide is still to run before handing off more.
MAX_BATCH_GLIDE = 128
BATCH_MIN_RATE = 5.0
BATCH_MAX_RATE = 30.0
BATCH_CATCH_UP_SECONDS = 2.5
ADVANCE_BATCH = 1
ADVANCE_FADE = 2
REVEAL_SWEEP = 0
REVEAL_CURTAIN = 1

# Region ids are per app; any constants will do. The transcript region, and
# the live panel's rows above a row just added to it.
REGION_ID = 7
PANEL_ID = 8
OFF = "".join(f"\x1b_smooth;off;{id}\x1b\\" for id in (REGION_ID, PANEL_ID))

# Reader-mode row marks (protocol 10), written at the start of the row they
# tag: a turn's submitted prompt, and the first row of a code block or diff.
MARK_TURN = 1
MARK_BLOCK = 2
# Arms a park: the next turn mark binds to it, and once that row scrolls off
# the top the terminal stops following output there and enters reader mode.
PARK = "\x1b_smooth;park\x1b\\"
# The turn's rows are all written: a park that never came due is dropped, so
# later output cannot park at an old prompt.
PARK_OFF = "\x1b_smooth;park;off\x1b\\"


def mark(kind: int) -> str:
    """Tag the row the cursor is on as a ``MARK_TURN`` or ``MARK_BLOCK``."""
    return f"\x1b_smooth;mark;{kind}\x1b\\"


# The terminal's spring glide (Ghostty Smooth's `State.Smooth`), mirrored so
# pcode knows when a panel region has settled.
GLIDE_STIFFNESS = 220.0
MAX_GLIDE = 3.0


class Glide:
    """pcode's copy of a spring glide: how many rows a region is still drawn off its place."""

    def __init__(self) -> None:
        self.rows = 0.0
        self.velocity = 0.0
        self.at = 0.0

    def _step(self, dt: float) -> tuple[float, float]:
        """`(rows, velocity)` after ``dt`` seconds, without mutating."""
        rows, vel = self.rows, self.velocity
        if dt <= 0.0 or (not rows and not vel):
            return rows, vel
        omega = math.sqrt(GLIDE_STIFFNESS)
        c = vel + omega * rows
        decay = math.exp(-omega * dt)
        new_rows = (rows + c * dt) * decay
        new_vel = (vel - omega * c * dt) * decay
        crossed = rows != 0.0 and (rows > 0.0) != (new_rows > 0.0)
        if crossed or (abs(new_rows) <= 0.001 and abs(new_vel) < 0.01):
            return 0.0, 0.0
        return new_rows, new_vel

    def remaining(self, now: float) -> float:
        """Rows still to glide at ``now``; negative while drawn higher than its place."""
        return self._step(max(0.0, now - self.at))[0]

    def advance(self, rows: int, now: float) -> None:
        """Content just moved up ``rows`` (negative: down): add them as the glide stands now."""
        pos, vel = self._step(max(0.0, now - self.at))
        self.rows = min(max(pos + rows, -MAX_GLIDE), MAX_GLIDE)
        self.velocity = vel if self.rows else 0.0
        self.at = now


def batch_rate(backlog: float) -> float:
    """The terminal's conveyor speed (rows/s) for ``backlog`` rows still to glide."""
    return min(BATCH_MAX_RATE, max(BATCH_MIN_RATE, backlog / BATCH_CATCH_UP_SECONDS))


def worth_asking(env=os.environ) -> bool:
    """Whether the terminal may be Ghostty Smooth, so the query is worth sending.

    Only Ghostty is asked: an unknown terminal might echo the query, and a
    reply that arrives after the startup probe gives up would otherwise be
    typed into the editor. tmux does not relay the sequences.
    """
    if env.get("TMUX"):
        return False
    # Not GHOSTTY_RESOURCES_DIR: terminals started from a Ghostty shell inherit it.
    return env.get("TERM") == "xterm-ghostty" or env.get("TERM_PROGRAM") == "ghostty"


def declaration(bottom: int, columns: int) -> str:
    """Rows 0 to ``bottom`` (0-based; ``bottom`` is the hidden overscan row).

    ``pos`` and ``max`` are 0: nothing for the terminal to scroll on the app's
    behalf, so trackpad scrolling through history stays the terminal's own.
    """
    return f"\x1b_smooth;{REGION_ID};0;{bottom};0;{columns - 1};0;0\x1b\\"


def visible_declaration(region: int, top: int, bottom: int, columns: int) -> str:
    """Rows ``top`` to ``bottom`` (0-based), all visible, as region ``region``.

    The no-overscan flag (bit 0 of the last field) keeps ``bottom`` drawn:
    these regions have something right under them (the status line, the
    typing row), so there is no blank row to hide. Rows glided past either
    edge are clipped there.
    """
    return f"\x1b_smooth;{region};{top};{bottom};0;{columns - 1};0;0;1\x1b\\"


def region_off(region: int) -> str:
    return f"\x1b_smooth;off;{region}\x1b\\"


def advance(rows: int, region: int = REGION_ID, flags: int = 0) -> str:
    """The region's content just moved up ``rows`` rows: glide it in.

    Plain, the terminal springs the region back into place (a few rows at
    most; it jumps the rest). ``ADVANCE_BATCH`` queues the rows on its
    conveyor instead, which adapts its speed to the backlog, and
    ``ADVANCE_FADE`` softens the region's bottom edge while it glides.
    """
    if flags:
        return f"\x1b_smooth;advance;{region};{rows};0;{flags}\x1b\\"
    return f"\x1b_smooth;advance;{region};{rows}\x1b\\"


def reveal(
    top: int,
    count: int,
    region: int = REGION_ID,
    rate: int = 0,
    flags: int = REVEAL_SWEEP,
) -> str:
    """Animate newly committed rows ``top .. top + count - 1`` inside ``region``.

    ``REVEAL_SWEEP`` runs a 120 Hz feathered horizontal ink sweep across each row
    in order; ``REVEAL_CURTAIN`` dissolves the block in top-to-bottom. Reveals
    sent back to back queue up and play in order. ``top`` may be negative for
    rows already scrolled above the region, within the batch glide's reach.
    ``rate`` 0 uses the terminal's default speed.
    """
    if flags:
        return f"\x1b_smooth;reveal;{region};{top};{count};{rate};{flags}\x1b\\"
    return f"\x1b_smooth;reveal;{region};{top};{count};{rate}\x1b\\"
