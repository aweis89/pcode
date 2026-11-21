"""Experimental: glide new scrollback rows into view with Ghostty Smooth's region shift.

Behind ``PCODE_SMOOTH_SCROLL=1``; see dev/ghostty-app-plan.md. Needs the
tlpinney/ghostty fork, whose smooth-scroll regions (its SMOOTH.md, protocol 3)
let an app declare a rectangle of rows and draw it shifted up by a fraction of
a row. Other terminals ignore the sequence.

The transcript rows above the editor are declared as one region whose last row
is the live row directly under scrollback (the typing row). That row is
hidden while declared, so the next row waiting for scrollback is drawn there
and a spring slides the region up into it. When the spring passes a whole row,
`TerminalOutput` writes that row to scrollback for real and re-declares the
region with the remaining fraction in the same synchronized frame, so the
motion continues without a jump.

Only while the editor is pinned to the bottom of the screen: before that,
a written row moves the editor down instead of scrolling, and there is
nothing to glide.
"""

import math
import os

# Critically damped: no overshoot. Higher is snappier.
STIFFNESS = 220.0
# Rows behind the newest before the display jumps instead of gliding, so a
# command log does not fall behind.
SNAP_ROWS = 8
TICK_SECONDS = 1 / 120
# Region ids are per app; any constant will do.
REGION_ID = 7
OFF = f"\x1b_smooth;off;{REGION_ID}\x1b\\"


def enabled() -> bool:
    """Opted in, and not under tmux, which does not relay the sequence."""
    return os.environ.get("PCODE_SMOOTH_SCROLL") == "1" and not os.environ.get("TMUX")


def debug_log():
    """A line-buffered file for ``PCODE_SMOOTH_SCROLL_LOG``, if set."""
    path = os.environ.get("PCODE_SMOOTH_SCROLL_LOG")
    return open(path, "a", buffering=1) if path else None  # noqa: SIM115


def declaration(bottom: int, columns: int, oy: int) -> str:
    """Rows 0 to ``bottom`` (0-based, ``bottom`` hidden) drawn ``oy``/1000 rows up.

    The shift form also makes the rightmost column overscan, hidden while the
    region is declared.
    """
    return f"\x1b_smooth;{REGION_ID};0;{bottom};0;{columns - 1};0;0;0;{oy}\x1b\\"


class Spring:
    """The display position, in absolute scrollback rows, chasing the newest ready row."""

    def __init__(self) -> None:
        self.position = 0.0
        self.velocity = 0.0

    def floor_at(self, base: int) -> None:
        """Rows written some other way (a drain, a replay) are already in view."""
        if self.position < base:
            self.position, self.velocity = float(base), 0.0

    def step(self, target: float, dt: float) -> None:
        """Advance by ``dt`` seconds; closed form, so exact at any frame rate."""
        if target - self.position > SNAP_ROWS:
            self.position = target - SNAP_ROWS
        omega = math.sqrt(STIFFNESS)
        offset = self.position - target
        decay = math.exp(-omega * dt)
        c = self.velocity + omega * offset
        self.position = target + (offset + c * dt) * decay
        self.velocity = (self.velocity - omega * c * dt) * decay
        if self.position > target or (
            abs(target - self.position) < 0.001 and abs(self.velocity) < 0.01
        ):
            self.position, self.velocity = target, 0.0

    def shift(self) -> int:
        """The fraction of a row past the last whole one, in thousandths."""
        return min(999, int((self.position - math.floor(self.position)) * 1000))
