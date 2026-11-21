"""Experimental: glide new scrollback rows into view in a patched Ghostty Smooth.

Behind ``PCODE_SMOOTH_SCROLL=1``; see dev/ghostty-app-plan.md. Needs the
tlpinney/ghostty fork with the ``advance`` command (smooth-scroll protocol 4,
the `pcode-glide` patch). Other terminals ignore the sequences.

The transcript rows above the editor are declared as one region whose last row
is the empty live row directly under scrollback (the typing row, kept as the
region's hidden overscan row). When pcode writes rows to scrollback while the
editor is pinned to the bottom, the screen scrolls, and pcode sends
``advance`` in the same synchronized frame. The terminal then draws the region
that many rows lower and springs it back into place on its own frame clock,
so pcode's busy event loop cannot make the motion stutter.
"""

import os

# Region ids are per app; any constant will do.
REGION_ID = 7
OFF = f"\x1b_smooth;off;{REGION_ID}\x1b\\"


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
