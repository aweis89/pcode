"""Experimental: glide new scrollback rows into view in a patched Ghostty Smooth.

Behind ``PCODE_SMOOTH_SCROLL=1``; see dev/ghostty-app-plan.md. Needs the
tlpinney/ghostty fork with the ``advance`` command (smooth-scroll protocol 4,
the `pcode-glide` patch). Other terminals ignore the sequences.

The transcript rows above the editor and the typing row under them (where prose
types out) are declared as one region, with one reserved blank row below as
the region's hidden overscan row. When pcode writes rows to scrollback while the
editor is pinned to the bottom, the screen scrolls, and pcode sends
``advance`` in the same synchronized frame. The terminal then draws the region
that many rows lower and springs it back into place on its own frame clock,
so pcode's busy event loop cannot make the motion stutter.
"""

import math
import os

# How fast rows are written while gliding, in rows per second: slow enough
# that each glides in before the next arrives. A backlog speeds it up to
# finish in about CATCH_UP_SECONDS, never past the maximum.
MIN_ROWS_PER_SECOND = 5.0
MAX_ROWS_PER_SECOND = float(os.environ.get("PCODE_SMOOTH_SCROLL_MAX_RATE") or 10)
CATCH_UP_SECONDS = 3.0

# Region ids are per app; any constant will do.
REGION_ID = 7
OFF = f"\x1b_smooth;off;{REGION_ID}\x1b\\"


# The terminal's glide spring (Ghostty Smooth's `Smooth.glide_stiffness` and
# `max_glide`), mirrored so pcode knows how far behind the screen still is.
GLIDE_STIFFNESS = 220.0
MAX_GLIDE = 3.0
# Typing waits until the row it types on is at least this close to its place,
# in rows, so text never types below what can be seen.
TYPE_WHEN_GLIDE_UNDER = 0.25


class Glide:
    """pcode's copy of the terminal's glide: how many rows the screen is still drawn low."""

    def __init__(self) -> None:
        self.rows = 0.0
        self.velocity = 0.0
        self.at = 0.0

    def remaining(self, now: float) -> float:
        """Rows still to glide at ``now``; closed form, as the terminal computes it."""
        if not self.rows and not self.velocity:
            return 0.0
        omega = math.sqrt(GLIDE_STIFFNESS)
        t = max(0.0, now - self.at)
        position = (self.rows + (self.velocity + omega * self.rows) * t) * math.exp(-omega * t)
        return max(0.0, position)

    def advance(self, rows: int, now: float) -> None:
        """The screen just scrolled ``rows``: add them to the glide as it stands now."""
        omega = math.sqrt(GLIDE_STIFFNESS)
        t = max(0.0, now - self.at)
        c = self.velocity + omega * self.rows
        decay = math.exp(-omega * t)
        position = (self.rows + c * t) * decay
        self.velocity = (self.velocity - omega * c * t) * decay
        self.rows = min(max(position, 0.0) + rows, MAX_GLIDE)
        self.at = now


class Pacer:
    """Rows due now under the glide's rate limit, from a credit that accrues over time."""

    def __init__(self) -> None:
        self.credit = 1.0
        self.last: float | None = None

    def allowance(self, backlog: int, now: float) -> int:
        """Rows ``backlog`` may write now (0 or 1); the first after a pause goes at once.

        Call `spend` with what was written.
        """
        if not backlog:
            self.credit, self.last = 1.0, None
            return 0
        rate = min(MAX_ROWS_PER_SECOND, max(MIN_ROWS_PER_SECOND, backlog / CATCH_UP_SECONDS))
        if self.last is not None:
            self.credit = min(self.credit + rate * (now - self.last), 1.0)
        self.last = now
        return int(self.credit)

    def spend(self, rows: int) -> None:
        self.credit -= rows


def enabled() -> bool:
    """Opted in, and not under tmux, which does not relay the sequences."""
    return os.environ.get("PCODE_SMOOTH_SCROLL") == "1" and not os.environ.get("TMUX")


def declaration(bottom: int, columns: int) -> str:
    """Rows 0 to ``bottom`` (0-based; ``bottom`` is the hidden overscan row).

    ``pos`` and ``max`` are 0: nothing for the terminal to scroll on the app's
    behalf, so trackpad scrolling through history stays the terminal's own.
    """
    return f"\x1b_smooth;{REGION_ID};0;{bottom};0;{columns - 1};0;0\x1b\\"


def advance(rows: int) -> str:
    """The region's content just moved up ``rows`` rows: glide it in."""
    return f"\x1b_smooth;advance;{REGION_ID};{rows}\x1b\\"
