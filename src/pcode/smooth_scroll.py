"""Experimental: glide new scrollback rows into view in a patched Ghostty Smooth.

The ``smooth_scroll`` setting turns it on (``auto``); see dev/ghostty-app-plan.md.
It needs the tlpinney/ghostty fork with the `pcode-glide` patch, which answers
the startup query (`pcode.theme`) with exactly ``PROTOCOL``. Anything else,
including tmux, which does not relay the sequences, gets none of them.

The transcript rows above the editor and the typing row under them (where prose
types out) are declared as one region, with one reserved blank row below as
the region's hidden overscan row. When pcode writes rows to scrollback while the
editor is pinned to the bottom, the screen scrolls, and pcode sends
``advance`` in the same synchronized frame. The terminal then draws the region
that many rows lower and glides it back into place on its own frame clock
(cruising at a steady rate while rows keep arriving, or springing back on
isolated moves), so pcode's busy event loop cannot make the motion stutter.
"""

import math
import os
import re

# How fast rows are written while gliding, in rows per second. A backlog speeds
# it up to finish in about CATCH_UP_SECONDS, never past the maximum.
MIN_ROWS_PER_SECOND = 5.0
MAX_ROWS_PER_SECOND = 10.0
CATCH_UP_SECONDS = 3.0

# The smooth-scroll protocol version pcode speaks: 4 added `advance`, 5 the
# no-overscan flag, 6 negative advances (content moved down) with the
# innermost of nested regions winning, 7 an optional rate hint on `advance`
# for steady-rate conveyor scrolling. Exact, since `Glide` mirrors the
# terminal's motion.
PROTOCOL = 7
QUERY = b"\x1b_smooth;?\x1b\\"
# Any smooth-scroll reply, so a future shape is still kept out of typing; only
# `ok;N` with N == PROTOCOL turns the glide on.
REPLY = re.compile(rb"\x1b_smooth;([^\x1b\x07]{0,32})\x1b\\")
VERSION = re.compile(rb"ok;(\d{1,4})")

# Region ids are per app; any constants will do. The transcript region, and
# the live panel's rows above a row just added to it.
REGION_ID = 7
PANEL_ID = 8
# Rows written before the screen is full, which push the typing row down
# instead of scrolling: nested in the transcript region, so it wins there.
INSERT_ID = 9
OFF = "".join(f"\x1b_smooth;off;{id}\x1b\\" for id in (REGION_ID, PANEL_ID, INSERT_ID))


# The terminal's glide dynamics (Ghostty Smooth's `State.Smooth`), mirrored so
# pcode knows how far behind the screen still is.
GLIDE_STIFFNESS = 220.0
MAX_GLIDE = 3.0
MAX_RATE_CENTI = 10_000
MIN_CONVEYOR_RATE = 0.5
TAU_RAMP = 0.14
TAU_BRAKE = 0.035
TAU_CRUISE = 0.35
BRAKE_GAIN = 7.0
CONVEYOR_SUBSTEP = 0.01
# Typing waits until the row it types on is at least this close to its place,
# in rows, so text never types below what can be seen.
TYPE_WHEN_GLIDE_UNDER = 0.25


class Glide:
    """pcode's copy of the terminal's glide: how many rows the screen is still drawn low."""

    def __init__(self) -> None:
        self.rows = 0.0
        self.velocity = 0.0
        self.rate = 0.0
        self.cruise = 0.0
        self.since = 0.0
        self.at = 0.0

    def _step_state(self, dt: float) -> tuple[float, float, float, float, float]:
        """Advance `(rows, velocity, rate, cruise, since)` by `dt` without mutating."""
        rows, vel = self.rows, self.velocity
        rate, cruise, since = self.rate, self.cruise, self.since
        if dt <= 0.0:
            return rows, vel, rate, cruise, since
        if not rows and not vel:
            return 0.0, 0.0, 0.0, 0.0, since
        if cruise == 0.0 or rows <= 0.0:
            omega = math.sqrt(GLIDE_STIFFNESS)
            c = vel + omega * rows
            decay = math.exp(-omega * dt)
            new_rows = (rows + c * dt) * decay
            new_vel = (vel - omega * c * dt) * decay
            crossed = rows != 0.0 and (rows > 0.0) != (new_rows > 0.0)
            if crossed or (abs(new_rows) <= 0.001 and abs(new_vel) < 0.01):
                return 0.0, 0.0, 0.0, 0.0, since
            return new_rows, new_vel, 0.0, 0.0, since

        rem_dt = min(dt, 10.0)
        while rem_dt > 0.0:
            h = min(rem_dt, CONVEYOR_SUBSTEP)
            rem_dt -= h
            since += h
            if rate > 0.0:
                timeout = min(4.0, max(0.45, 2.0 / rate))
                if since > timeout:
                    rate = 0.0
            if rate > 0.0:
                cruise += (rate - cruise) * (1.0 - math.exp(-h / TAU_CRUISE))
                v_des = cruise
                cushion_hi = min(MAX_GLIDE - 0.65, max(1.4, cruise * TAU_RAMP + 1.05))
                if rows > cushion_hi:
                    v_des += 3.0 * (rows - cushion_hi)
            else:
                v_des = cruise
            v_des = min(v_des, BRAKE_GAIN * rows)
            prev_speed = max(0.0, -vel)
            tau = TAU_RAMP if v_des >= prev_speed else TAU_BRAKE
            speed = prev_speed + (v_des - prev_speed) * (1.0 - math.exp(-h / tau))
            vel = -speed
            rows -= 0.5 * (prev_speed + speed) * h
            if rows <= 0.001:
                return 0.0, 0.0, 0.0, 0.0, since
        return rows, vel, rate, cruise, since

    def remaining(self, now: float) -> float:
        """Rows still to glide at ``now``; negative while drawn higher than its place."""
        return self._step_state(max(0.0, now - self.at))[0]

    def advance(self, rows: int, now: float, rate: float = 0.0) -> None:
        """Content just moved up ``rows`` (negative: down): add them as the glide stands now."""
        pos, vel, _, cruise, _ = self._step_state(max(0.0, now - self.at))
        quantized = _rate_centi(rate) / 100.0
        self.rows = min(max(pos + rows, -MAX_GLIDE), MAX_GLIDE)
        self.velocity = vel
        self.since = 0.0
        if self.rows == 0.0:
            self.velocity = 0.0
            self.rate = 0.0
            self.cruise = 0.0
        elif self.rows < 0.0:
            self.rate = 0.0
            self.cruise = 0.0
        elif rows > 0 and quantized >= MIN_CONVEYOR_RATE:
            self.rate = quantized
            self.cruise = cruise if cruise > 0.0 else quantized
        else:
            self.rate = 0.0
            self.cruise = cruise
        self.at = now


class Pacer:
    """Rows due now under the glide's rate limit, from a credit that accrues over time."""

    def __init__(self) -> None:
        self.credit = 1.0
        self.last: float | None = None

    @staticmethod
    def rate_for(backlog: int) -> float:
        """Target row rate (rows/s) for a queue of ``backlog`` rows."""
        if backlog <= 0:
            return 0.0
        return min(MAX_ROWS_PER_SECOND, max(MIN_ROWS_PER_SECOND, backlog / CATCH_UP_SECONDS))

    def allowance(self, backlog: int, now: float) -> int:
        """Rows ``backlog`` may write now (0 or 1); the first after a pause goes at once.

        Call `spend` with what was written. Preserves fractional credit across
        regular frame ticks so 30 Hz quantization does not drag the rate down.
        """
        if not backlog:
            self.credit, self.last = 1.0, None
            return 0
        rate = self.rate_for(backlog)
        if self.last is not None:
            dt = max(0.0, now - self.last)
            if self.credit < 1.0:
                cap = 1.0 if dt > 0.1 else 1.0 + rate * min(dt, 0.05)
                self.credit = min(self.credit + rate * dt, cap)
            else:
                self.credit = 1.0
        self.last = now
        return 1 if self.credit >= 1.0 else 0

    def spend(self, rows: int) -> None:
        self.credit -= rows


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


def _rate_centi(rate: float) -> int:
    """Quantize a rate hint (rows/s) to the protocol's centi-rows/s integer."""
    if rate <= 0.0:
        return 0
    return min(MAX_RATE_CENTI, max(1, round(rate * 100)))


def advance(rows: int, region: int = REGION_ID, rate: float = 0.0) -> str:
    """The region's content just moved up ``rows`` rows: glide it in.

    When ``rate`` is positive (rows per second), the terminal cruises at that
    steady speed while more rows follow instead of braking between each row.
    """
    centi = _rate_centi(rate)
    if centi:
        return f"\x1b_smooth;advance;{region};{rows};{centi}\x1b\\"
    return f"\x1b_smooth;advance;{region};{rows}\x1b\\"
