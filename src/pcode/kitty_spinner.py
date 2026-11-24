"""Smooth terminal-clocked spinners via Kitty Graphics Protocol animations.

Uploads tiny anti-aliased RGBA animation frames once and places them inline as
Unicode placeholders (`U+10EEEE`, with explicit row/column diacritics for
multi-cell hero spinners), with the image ID encoded in the cell's 24-bit
foreground color (`fg:#rrggbb`). Ghostty and kitty advance the frames on their
own clock, so animations stay fluid while Python repaints far less often.
Terminals without Kitty graphics, multiplexers (`TMUX`/`STY`), non-truecolor
modes, ASCII glyph mode, or `smooth_spinner = off` fall back to Rich's text
spinners transparently.
"""

from __future__ import annotations

import asyncio
import base64
import math
import os
import re
import zlib
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from functools import cache, lru_cache
from typing import Any

from pcode import glyphs
from pcode.terminal_color import true_color

TAU = 2.0 * math.pi
PLACEHOLDER = "\U0010eeee"
# Kitty Unicode placeholder row/column diacritics (indices 0..3).
DIACRITICS = ("\u0305", "\u030d", "\u030e", "\u0310")

# Standard Kitty graphics query (id 31 does not store an image). Sent ahead of
# OSC 11 during startup detection so a terminal that speaks Kitty graphics
# replies `\x1b_Gi=31;OK\x1b\\` before the background answer ends the wait.
QUERY = b"\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\"
REPLY = re.compile(rb"\x1b_Gi=31;([^\x07\x1b]{0,64})\x1b\\")

# Cell image dimensions (1:2 aspect ratio matches typical monospace cells).
CELL_W = 28
CELL_H = 56


@dataclass(frozen=True)
class StyleSpec:
    """Timing, Rich fallback, and natural cell width for a smooth spinner style."""

    period: float
    frames: int
    rich_fallback: str
    cols: int = 1


@dataclass(frozen=True)
class SlotSpec:
    """Render specification for one uploaded spinner slot."""

    style: str
    cols: int
    rgb1: tuple[int, int, int]
    rgb2: tuple[int, int, int]
    is_light: bool = False
    # Drawn toward the left of a 2x1 box, so it sits over a 1-cell icon
    # column and its right cell is the gap before the row's text.
    inset: bool = False


STYLES: dict[str, StyleSpec] = {
    "helix": StyleSpec(2.3, 84, "dots", cols=2),
    "gyro": StyleSpec(2.8, 92, "dots", cols=2),
    "octa": StyleSpec(2.6, 88, "dots"),
    "swept": StyleSpec(TAU / 2.4, 96, "dots"),
    "dual": StyleSpec(TAU / 1.8, 112, "arc"),
    "orbit": StyleSpec(TAU / 3.9, 64, "dots"),
    "wave": StyleSpec(TAU / 5.4, 48, "dots"),
    "comet": StyleSpec(TAU / 3.1, 80, "dots"),
}
SMOOTH_NAMES: tuple[str, ...] = tuple(STYLES)
DEFAULT_STYLE: dict[str, str] = {"spinner": "helix", "tool_spinner": "dual"}

# High 24-bit image IDs ("pc\x01".."pc\x07") so they never collide with user
# images displayed in the same terminal.
SLOTS: dict[str, int] = {
    "status": 0x706301,
    "tool": 0x706302,
    "accent": 0x706303,
    "aside": 0x706304,
    "startup": 0x706305,
    "task": 0x706306,
    "agent": 0x706307,
}

# prompt_toolkit style classes encoding each slot's 24-bit image ID in fg RGB.
# No `bg:` is set, so cells inherit shaded-box backgrounds automatically.
STYLE_CLASSES: dict[str, str] = {
    f"kitty.spinner.{slot}": f"fg:#{img_id:06x} nodim nobold noreverse"
    for slot, img_id in SLOTS.items()
}

_HEX_RE = re.compile(r"#([0-9a-fA-F]{6})\b")


def worth_asking(env: Mapping[str, str] | None = None) -> bool:
    """Whether the terminal is known to support Kitty animations + U+10EEEE placeholders.

    Only Ghostty and kitty are asked (outside tmux/screen and with 24-bit color),
    because other terminals either lack virtual placements/animations or may
    echo unknown APC sequences. iTerm2 is not one of them despite claiming
    Kitty graphics: 3.5/3.6 answer the query `OK` without placeholders, and a
    3.7 nightly (2026-11) draws the placeholder but never advances its frames,
    so the spinner sits frozen.
    """
    env = os.environ if env is None else env
    if env.get("TMUX") or env.get("STY"):
        return False
    if not true_color(env):
        return False
    term = env.get("TERM", "")
    prog = env.get("TERM_PROGRAM", "")
    return (
        term in ("xterm-ghostty", "xterm-kitty")
        or prog == "ghostty"
        or bool(env.get("KITTY_WINDOW_ID"))
    )


def rich_fallback(name: str) -> str:
    """Rich spinner name to use when `name` is a smooth-only spinner style."""
    spec = STYLES.get(name)
    return spec.rich_fallback if spec is not None else name


def style_cols(name: str) -> int:
    """Natural cell width of smooth spinner `name` on wide border slots."""
    spec = STYLES.get(name)
    return spec.cols if spec is not None else 1


def placeholder_text(cols: int = 1) -> str:
    """Unicode placeholder cells for a 1-row, `cols`-wide Kitty virtual placement.

    Single-cell placements use bare `U+10EEEE`; multi-cell placements attach
    explicit row-0 and column-k diacritics so partial screen repaints keep
    every cell anchored to its slice of the image.
    """
    if cols <= 1:
        return PLACEHOLDER
    if cols > len(DIACRITICS):
        raise ValueError(f"cols={cols} exceeds supported diacritics ({len(DIACRITICS)})")
    return "".join(f"{PLACEHOLDER}{DIACRITICS[0]}{DIACRITICS[c]}" for c in range(cols))


# An inset slot's placeholder: a 2x1 image whose right cell stands in for the
# space before the row's text, so the text lines up with rows whose icon is
# one cell. That leaves room for a spinner as tall as `✔`, which 1x1 is not.
INSET_ICON = placeholder_text(2)
# An inset image's centre, in pixels from its left edge.
INSET_CX = 21.5
# Styles with an inset 2x1 shape.
INSET_STYLES = frozenset({"dual", "gyro", "helix"})


def smooth_style_for(setting: str, configured: str, default: str) -> str | None:
    """Smooth style name for `setting`, or None to honor a custom Rich spinner."""
    if configured in STYLES:
        return configured
    if configured == default:
        return DEFAULT_STYLE.get(setting)
    return None


def resolve_rgb(color: str, fallback_hex: str) -> tuple[int, int, int]:
    """Parse a prompt_toolkit palette color string into an (R, G, B) tuple.

    Explicit `#rrggbb` colors win; ANSI palette names (`/syntax terminal`) use
    `fallback_hex` from the active dark/light palette so spinners keep proper
    contrast across both dark and light themes.
    """
    match = _HEX_RE.search(color)
    if match is not None:
        hx = match.group(1)
        return int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    match = _HEX_RE.search(fallback_hex)
    if match is not None:
        hx = match.group(1)
        return int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    return (196, 181, 253)


def _blend_u8(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    it = 1.0 - t
    return (
        int(round(a[0] * it + b[0] * t)),
        int(round(a[1] * it + b[1] * t)),
        int(round(a[2] * it + b[2] * t)),
    )


def _is_light_palette(palette) -> bool:
    surface = getattr(palette, "surface", "#242933")
    r, g, b = resolve_rgb(surface, "#242933")
    return (0.299 * r + 0.587 * g + 0.114 * b) > 128.0


def _norm(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    return (rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)


def _mix_rgb(
    a: tuple[float, float, float], b: tuple[float, float, float], t: float
) -> tuple[float, float, float]:
    it = 1.0 - t
    return (a[0] * it + b[0] * t, a[1] * it + b[1] * t, a[2] * it + b[2] * t)


def _lit_rgb(
    c: tuple[float, float, float], amount: float, is_light: bool
) -> tuple[float, float, float]:
    if is_light:
        target = (c[0] * 0.55, c[1] * 0.55, c[2] * 0.55)
        return _mix_rgb(c, target, amount * 0.65)
    return _mix_rgb(c, (1.0, 1.0, 1.0), amount)


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = (x - edge0) / (edge1 - edge0)
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return t * t * (3.0 - 2.0 * t)


def _sd_arc_cap(
    px: float,
    py: float,
    dist: float,
    ang: float,
    r: float,
    w: float,
    head: float,
    sweep: float,
) -> tuple[float, float]:
    back = (head - ang) % TAU
    if back <= sweep:
        return abs(dist - r) - w, back / sweep
    hx = math.cos(head) * r
    hy = math.sin(head) * r
    tail = head - sweep
    tx = math.cos(tail) * r
    ty = math.sin(tail) * r
    dh = math.hypot(px - hx, py - hy) - w
    dt = math.hypot(px - tx, py - ty) - w
    if dh <= dt:
        return dh, 0.0
    return dt, 1.0


class _Canvas:
    """Premultiplied RGBA canvas with bounding-box primitive stamping."""

    __slots__ = ("w", "h", "cx", "cy", "scale", "aa", "pr", "pg", "pb", "pa", "ymin", "ymax")

    def __init__(self, cols: int) -> None:
        self.w = cols * CELL_W
        self.h = CELL_H
        self.cx = 0.5 * self.w
        self.cy = 0.50 * self.h
        self.scale = 0.5 * CELL_W
        self.aa = 1.55 / self.scale
        n = self.w * self.h
        self.pr = [0.0] * n
        self.pg = [0.0] * n
        self.pb = [0.0] * n
        self.pa = [0.0] * n
        self.ymin = self.h
        self.ymax = 0

    def stamp_disc(
        self,
        x0: float,
        y0: float,
        radius: float,
        rgb: tuple[float, float, float],
        alpha: float,
        *,
        specular: float = 0.0,
        is_light: bool = False,
        glow: float = 0.0,
    ) -> None:
        if alpha <= 0.002:
            return
        scale = self.scale
        aa = self.aa
        cx, cy = self.cx, self.cy
        w, h = self.w, self.h
        reach = radius * (1.85 if glow > 0.0 else 1.0) + aa
        ix0 = max(0, int(cx + (x0 - reach) * scale))
        ix1 = min(w, int(cx + (x0 + reach) * scale) + 2)
        iy0 = max(0, int(cy + (y0 - reach) * scale))
        iy1 = min(h, int(cy + (y0 + reach) * scale) + 2)
        if ix0 >= ix1 or iy0 >= iy1:
            return
        if iy0 < self.ymin:
            self.ymin = iy0
        if iy1 > self.ymax:
            self.ymax = iy1

        pr, pg, pb, pa = self.pr, self.pg, self.pb, self.pa
        sr_base, sg_base, sb_base = rgb
        inv_s = 1.0 / scale
        spec_x = x0 - radius * 0.28
        spec_y = y0 - radius * 0.28
        spec_r = max(radius * 0.55, 0.05)

        for iy in range(iy0, iy1):
            py = (iy + 0.5 - cy) * inv_s
            dy = py - y0
            dy2 = dy * dy
            row = iy * w
            for ix in range(ix0, ix1):
                px = (ix + 0.5 - cx) * inv_s
                dx = px - x0
                dist = math.sqrt(dx * dx + dy2)
                d = dist - radius
                a = _smoothstep(aa, -aa, d) * alpha
                if glow > 0.0 and dist < reach:
                    t = 1.0 - dist / reach
                    ga = (t * t) * glow * alpha
                    if ga > a:
                        a = ga
                if a <= 0.002:
                    continue
                if specular > 0.0 and d < aa:
                    sd = math.hypot(px - spec_x, py - spec_y) / spec_r
                    sp = max(0.0, 1.0 - sd)
                    sp = sp * sp * specular
                    if sp > 0.005:
                        sr, sg, sb = _lit_rgb(rgb, sp, is_light)
                    else:
                        sr, sg, sb = sr_base, sg_base, sb_base
                else:
                    sr, sg, sb = sr_base, sg_base, sb_base
                idx = row + ix
                inv = 1.0 - a
                pr[idx] = sr * a + pr[idx] * inv
                pg[idx] = sg * a + pg[idx] * inv
                pb[idx] = sb * a + pb[idx] * inv
                pa[idx] = a + pa[idx] * inv

    def stamp_capsule(
        self,
        ax: float,
        ay: float,
        bx: float,
        by: float,
        radius: float,
        rgb: tuple[float, float, float],
        alpha: float,
    ) -> None:
        if alpha <= 0.002:
            return
        scale = self.scale
        aa = self.aa
        cx, cy = self.cx, self.cy
        w, h = self.w, self.h
        pad = radius + aa
        ix0 = max(0, int(cx + (min(ax, bx) - pad) * scale))
        ix1 = min(w, int(cx + (max(ax, bx) + pad) * scale) + 2)
        iy0 = max(0, int(cy + (min(ay, by) - pad) * scale))
        iy1 = min(h, int(cy + (max(ay, by) + pad) * scale) + 2)
        if ix0 >= ix1 or iy0 >= iy1:
            return
        if iy0 < self.ymin:
            self.ymin = iy0
        if iy1 > self.ymax:
            self.ymax = iy1

        abx = bx - ax
        aby = by - ay
        ab2 = abx * abx + aby * aby
        inv_ab2 = (1.0 / ab2) if ab2 > 1e-9 else 0.0
        pr, pg, pb, pa = self.pr, self.pg, self.pb, self.pa
        sr, sg, sb = rgb
        inv_s = 1.0 / scale

        for iy in range(iy0, iy1):
            py = (iy + 0.5 - cy) * inv_s
            pay = py - ay
            row = iy * w
            for ix in range(ix0, ix1):
                px = (ix + 0.5 - cx) * inv_s
                pax = px - ax
                t = (pax * abx + pay * aby) * inv_ab2
                if t < 0.0:
                    t = 0.0
                elif t > 1.0:
                    t = 1.0
                dx = pax - abx * t
                dy = pay - aby * t
                d = math.sqrt(dx * dx + dy * dy) - radius
                a = _smoothstep(aa, -aa, d) * alpha
                if a <= 0.002:
                    continue
                idx = row + ix
                inv = 1.0 - a
                pr[idx] = sr * a + pr[idx] * inv
                pg[idx] = sg * a + pg[idx] * inv
                pb[idx] = sb * a + pb[idx] * inv
                pa[idx] = a + pa[idx] * inv

    def to_rgba(self) -> bytes:
        w, h = self.w, self.h
        if self.ymin >= self.ymax:
            return b"\x00" * (w * h * 4)
        pr, pg, pb, pa = self.pr, self.pg, self.pb, self.pa
        y0, y1 = self.ymin, self.ymax
        top_pad = b"\x00" * (y0 * w * 4)
        bot_pad = b"\x00" * ((h - y1) * w * 4)
        buf = bytearray((y1 - y0) * w * 4)
        idx = 0
        for i in range(y0 * w, y1 * w):
            a = pa[i]
            if a > 0.002:
                inv_a = 255.0 / a
                r = int(pr[i] * inv_a + 0.5)
                g = int(pg[i] * inv_a + 0.5)
                b = int(pb[i] * inv_a + 0.5)
                buf[idx] = 255 if r > 255 else r
                buf[idx + 1] = 255 if g > 255 else g
                buf[idx + 2] = 255 if b > 255 else b
                av = int(a * 255.0 + 0.5)
                buf[idx + 3] = 255 if av > 255 else av
            idx += 4
        return top_pad + bytes(buf) + bot_pad


def _grid_1x1() -> tuple[int, int, tuple[tuple[float, float, float, float], ...], float]:
    return _grid(1, 0.5 * CELL_W, 1.42, 1.55)


@cache
def _grid(
    cols: int, cx: float, reach: float, aa_px: float
) -> tuple[int, int, tuple[tuple[float, float, float, float], ...], float]:
    """Polar coordinates of a `cols`-wide box's rows within `reach` of its centre.

    `cx` is the centre's pixel column, and distances are in half-cell widths.
    """
    cy = 0.50 * CELL_H
    half_s = 0.5 * CELL_W
    aa = aa_px / half_s
    y_min = max(0, int(cy - half_s * reach))
    y_max = min(CELL_H, int(cy + half_s * reach) + 1)
    coords: list[tuple[float, float, float, float]] = []
    for y in range(y_min, y_max):
        py = (y + 0.5 - cy) / half_s
        for x in range(cols * CELL_W):
            px = (x + 0.5 - cx) / half_s
            coords.append((px, py, math.hypot(px, py), math.atan2(py, px)))
    return y_min, y_max, tuple(coords), aa


def _render_helix(
    phase: float,
    c1: tuple[float, float, float],
    c2: tuple[float, float, float],
    is_light: bool,
    cols: int,
    inset: bool = False,
) -> bytes:
    """Horizontal DNA helix (5 paired glossy beads at 2x1, compact 3-pair at 1x1)."""
    cv = _Canvas(cols)
    if cols >= 2 and inset:
        # Four pairs, as far apart as five, in the room an inset leaves.
        cv.cx = INSET_CX
        n_pairs, span, amp, phase_step, r_back, r_span = 4, 1.0, 0.84, 0.98, 0.16, 0.135
    elif cols >= 2:
        n_pairs, span, amp, phase_step, r_back, r_span = 5, 1.54, 0.84, 0.78, 0.16, 0.135
    else:
        n_pairs, span, amp, phase_step, r_back, r_span = 3, 0.62, 0.78, 1.05, 0.13, 0.10

    prims: list[tuple[float, tuple[Any, ...]]] = []
    for k in range(n_pairs):
        frac = k / (n_pairs - 1) if n_pairs > 1 else 0.5
        x = -span + 2.0 * span * frac
        phi = phase * TAU - k * phase_step
        ya = amp * math.sin(phi)
        za = math.cos(phi)
        for y, z, col in ((ya, za, c1), (-ya, -za, c2)):
            depth = 0.5 + 0.5 * z
            rad = r_back + r_span * depth
            alpha = 0.24 + 0.76 * (depth**1.15)
            spec = 0.45 * (depth**1.4)
            prims.append((z, (x, y, rad, col, alpha, spec, 0.26 * depth)))

    prims.sort(key=lambda item: item[0])
    for _, (x, y, rad, col, alpha, spec, gl) in prims:
        cv.stamp_disc(x, y, rad, col, alpha, specular=spec, is_light=is_light, glow=gl)
    return cv.to_rgba()


def _render_gyro(
    phase: float,
    c1: tuple[float, float, float],
    c2: tuple[float, float, float],
    is_light: bool,
    cols: int,
    inset: bool = False,
) -> bytes:
    """3D counter-precessing orbital rings + glowing core (2x1 square or compact 1x1)."""
    cv = _Canvas(cols)
    ang = phase * TAU
    wide = cols >= 2
    r_ring = 0.74
    if wide:
        r_ring = 1.32
    if wide and inset:
        # Smaller, to leave the gap before the row's text.
        cv.cx, r_ring = INSET_CX, 1.18
    steps = 28 if wide else 22
    prims: list[tuple[float, str, tuple[Any, ...]]] = []

    pulse = 0.5 + 0.5 * math.sin(ang * 2.0)
    core_col = _lit_rgb(_mix_rgb(c1, c2, 0.5), 0.28 + 0.22 * pulse, is_light)
    core_r = (0.28 + 0.08 * pulse) if wide else (0.18 + 0.045 * pulse)
    prims.append((0.0, "dot", (0.0, 0.0, core_r, core_col, 0.85, 0.45, 0.22)))

    rings = (
        (c1, ang, 0.92, 0.38, ang * 2.0),
        (c2, -ang + 0.8, 1.02, -0.58, -ang * 2.0 + 1.5),
    )
    for col, yaw, tilt_x, roll, bead_ang in rings:
        cyw, syw = math.cos(yaw), math.sin(yaw)
        ctx, stx = math.cos(tilt_x), math.sin(tilt_x)
        crl, srl = math.cos(roll), math.sin(roll)

        def project(theta: float) -> tuple[float, float, float]:
            x0 = r_ring * math.cos(theta)
            y0 = r_ring * math.sin(theta)
            y1 = y0 * ctx
            z1 = y0 * stx
            x2 = x0 * cyw + z1 * syw
            z2 = -x0 * syw + z1 * cyw
            x3 = x2 * crl - y1 * srl
            y3 = x2 * srl + y1 * crl
            return x3, y3, z2 / r_ring

        pts = [project((i / steps) * TAU) for i in range(steps + 1)]
        for i in range(steps):
            ax, ay, az = pts[i]
            bx, by, bz = pts[i + 1]
            zm = 0.5 * (az + bz)
            depth = 0.5 + 0.5 * zm
            rad = (0.065 + 0.065 * depth) if wide else (0.048 + 0.040 * depth)
            alpha = 0.22 + 0.74 * (depth**1.25)
            prims.append((zm, "seg", (ax, ay, bx, by, rad, col, alpha)))

        bx, by, bz = project(bead_ang)
        bdepth = 0.5 + 0.5 * bz
        bcol = _lit_rgb(col, 0.35 * bdepth, is_light)
        brad = (0.14 + 0.11 * bdepth) if wide else (0.095 + 0.055 * bdepth)
        prims.append((bz + 0.05, "dot", (bx, by, brad, bcol, 0.35 + 0.65 * bdepth, 0.4, 0.20)))

    prims.sort(key=lambda item: item[0])
    for _, kind, args in prims:
        if kind == "seg":
            cv.stamp_capsule(*args)
        else:
            x, y, rad, col, alpha, spec, gl = args
            cv.stamp_disc(x, y, rad, col, alpha, specular=spec, is_light=is_light, glow=gl)
    return cv.to_rgba()


def _render_octa(
    phase: float,
    c1: tuple[float, float, float],
    c2: tuple[float, float, float],
    is_light: bool,
) -> bytes:
    """1x1 3D tumbling wireframe octahedron with Z-depth."""
    cv = _Canvas(1)
    ay = phase * TAU
    ax = 0.58 + 0.28 * math.sin(phase * TAU)
    cy, sy = math.cos(ay), math.sin(ay)
    cx, sx = math.cos(ax), math.sin(ax)
    scale = 0.76

    raw_verts = (
        (0.0, -1.24, 0.0),
        (0.0, 1.24, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0),
        (-1.0, 0.0, 0.0),
        (0.0, 0.0, -1.0),
    )
    verts: list[tuple[float, float, float]] = []
    for vx, vy, vz in raw_verts:
        x1 = vx * cy + vz * sy
        z1 = -vx * sy + vz * cy
        y2 = vy * cx - z1 * sx
        z2 = vy * sx + z1 * cx
        verts.append((x1 * scale, y2 * scale, z2))

    edges = (
        (0, 2),
        (0, 3),
        (0, 4),
        (0, 5),
        (1, 2),
        (1, 3),
        (1, 4),
        (1, 5),
        (2, 3),
        (3, 4),
        (4, 5),
        (5, 2),
    )
    prims: list[tuple[float, str, tuple[Any, ...]]] = []
    for i, j in edges:
        ax_, ay_, az_ = verts[i]
        bx_, by_, bz_ = verts[j]
        zm = 0.5 * (az_ + bz_)
        depth = max(0.0, min(1.0, 0.5 + 0.45 * zm))
        col = _mix_rgb(c2, c1, depth)
        prims.append(
            (zm, "edge", (ax_, ay_, bx_, by_, 0.048 + 0.040 * depth, col, 0.22 + 0.76 * depth))
        )
    for i, (vx, vy, vz) in enumerate(verts):
        depth = max(0.0, min(1.0, 0.5 + 0.45 * vz))
        col = c1 if i < 2 else c2
        prims.append(
            (
                vz + 0.05,
                "vert",
                (vx, vy, 0.075 + 0.065 * depth, col, 0.35 + 0.65 * depth, 0.45 * depth),
            )
        )

    prims.sort(key=lambda item: item[0])
    for _, kind, args in prims:
        if kind == "edge":
            cv.stamp_capsule(*args)
        else:
            vx, vy, r, col, alpha, spec = args
            cv.stamp_disc(vx, vy, r, col, alpha, specular=spec, is_light=is_light)
    return cv.to_rgba()


def _render_swept(
    t_sec: float,
    c1: tuple[float, float, float],
    c2: tuple[float, float, float],
    is_light: bool,
) -> bytes:
    """1x1 breathing tapered arc with dual-tone tail, lit head, and subtle track."""
    y_min, y_max, coords, aa = _grid_1x1()
    top_pad = b"\x00" * (y_min * CELL_W * 4)
    bot_pad = b"\x00" * ((CELL_H - y_max) * CELL_W * 4)
    buf = bytearray((y_max - y_min) * CELL_W * 4)
    idx = 0
    head = t_sec * 4.8 + 0.75 * math.sin(t_sec * 2.4)
    sweep = 2.35 + 1.15 * math.sin(t_sec * 2.4 - 1.0)
    r, w = 0.76, 0.13
    hx, hy = math.cos(head) * r, math.sin(head) * r
    track_col = _mix_rgb(c1, c2, 0.5)
    bloom_col = _lit_rgb(c1, 0.35, is_light)
    for px, py, dist, ang in coords:
        d, frac = _sd_arc_cap(px, py, dist, ang, r, w, head, sweep)
        arc = _smoothstep(aa, -aa, d) * (1.0 - 0.84 * frac)
        track = _smoothstep(aa, -aa, abs(dist - r) - w * 0.58) * 0.16
        tip_d = math.hypot(px - hx, py - hy)
        bloom = max(0.0, 1.0 - tip_d / 0.34) ** 2 * 0.32
        a = max(arc, track, bloom)
        if a > 0.002:
            if arc >= track and arc >= bloom:
                col = _mix_rgb(c1, c2, frac * 0.65)
                lit = (max(0.0, 1.0 - frac * 3.5) ** 1.5) * 0.48
                if lit > 0.01:
                    col = _lit_rgb(col, lit, is_light)
            elif bloom > track:
                col = bloom_col
            else:
                col = track_col
            buf[idx] = min(255, int(col[0] * 255.0 + 0.5))
            buf[idx + 1] = min(255, int(col[1] * 255.0 + 0.5))
            buf[idx + 2] = min(255, int(col[2] * 255.0 + 0.5))
            buf[idx + 3] = min(255, int(min(1.0, a) * 255.0 + 0.5))
        idx += 4
    return top_pad + bytes(buf) + bot_pad


@dataclass(frozen=True)
class _DualShape:
    """Where `dual` sits in its box and how heavy its arcs are."""

    cx: float  # centre, in pixels from the box's left edge
    reach: float  # outer extent, anti-aliasing included, in half-cell widths
    aa_px: float
    r1: float  # outer arc radius and half-thickness
    w1: float
    r2: float  # inner arc
    w2: float
    fade1: float  # how far each tail fades
    fade2: float


_DUAL_CELL = _DualShape(0.5 * CELL_W, 1.42, 1.55, 0.79, 0.095, 0.45, 0.090, 0.76, 0.65)
# About 70% of the cell's height, like `✔`, with heavier arcs to match it.
_DUAL_INSET = _DualShape(INSET_CX, 1.47, 1.10, 1.25, 0.135, 0.712, 0.125, 0.50, 0.45)


def _render_dual(
    t_sec: float,
    c1: tuple[float, float, float],
    c2: tuple[float, float, float],
    is_light: bool,
    cols: int = 1,
    inset: bool = False,
) -> bytes:
    """Counter-rotating dual crescents with subtle dual-tone and lit tips."""
    shape = _DUAL_INSET if inset and cols >= 2 else _DUAL_CELL
    y_min, y_max, coords, aa = _grid(cols, shape.cx, shape.reach, shape.aa_px)
    width = cols * CELL_W
    top_pad = b"\x00" * (y_min * width * 4)
    bot_pad = b"\x00" * ((CELL_H - y_max) * width * 4)
    buf = bytearray((y_max - y_min) * width * 4)
    idx = 0
    h1, h2 = t_sec * 3.6, -t_sec * 5.4
    r1, w1, r2, w2 = shape.r1, shape.w1, shape.r2, shape.w2
    fade1, fade2 = shape.fade1, shape.fade2
    for px, py, dist, ang in coords:
        d1, f1 = _sd_arc_cap(px, py, dist, ang, r1, w1, h1, 2.65)
        d2, f2 = _sd_arc_cap(px, py, dist, ang, r2, w2, h2, 2.45)
        a1 = _smoothstep(aa, -aa, d1) * (1.0 - fade1 * f1)
        a2 = _smoothstep(aa, -aa, d2) * (0.90 - fade2 * f2)
        if a1 > 0.002 or a2 > 0.002:
            if a1 >= a2:
                a = a1
                col = _lit_rgb(c1, max(0.0, 1.0 - f1 * 3.2) * 0.42, is_light)
            else:
                a = a2
                col = _lit_rgb(c2, max(0.0, 1.0 - f2 * 3.2) * 0.42, is_light)
            buf[idx] = min(255, int(col[0] * 255.0 + 0.5))
            buf[idx + 1] = min(255, int(col[1] * 255.0 + 0.5))
            buf[idx + 2] = min(255, int(col[2] * 255.0 + 0.5))
            buf[idx + 3] = min(255, int(min(1.0, a) * 255.0 + 0.5))
        idx += 4
    return top_pad + bytes(buf) + bot_pad


def _render_orbit(
    t_sec: float,
    c1: tuple[float, float, float],
    c2: tuple[float, float, float],
    is_light: bool,
) -> bytes:
    """1x1 rubber-band chasing glossy beads with dual-tone tail."""
    cv = _Canvas(1)
    r = 0.68
    for k in range(3, -1, -1):
        fk = float(k)
        frac = fk / 3.0
        base = t_sec * 3.9 - fk * 0.48
        ang = base + 0.42 * math.sin(base)
        col = _mix_rgb(c1, c2, frac * 0.85)
        rad = 0.215 - 0.105 * frac
        alpha = 1.0 - 0.64 * frac
        spec = 0.45 * (1.0 - frac)
        glow = 0.20 * (1.0 - frac)
        cv.stamp_disc(
            math.cos(ang) * r,
            math.sin(ang) * r,
            rad,
            col,
            alpha,
            specular=spec,
            is_light=is_light,
            glow=glow,
        )
    return cv.to_rgba()


def _render_wave(
    t_sec: float,
    c1: tuple[float, float, float],
    c2: tuple[float, float, float],
    is_light: bool,
) -> bytes:
    """1x1 undulating vertical equalizer pills using the cell's 1:2 height."""
    cv = _Canvas(1)
    for k in range(3):
        cx = (float(k) - 1.0) * 0.62
        wave = 0.5 + 0.5 * math.sin(t_sec * 5.4 - float(k) * 1.15)
        h = 0.24 + 0.92 * wave
        col = _lit_rgb(_mix_rgb(c2, c1, wave), wave * 0.30, is_light)
        cv.stamp_capsule(cx, -h, cx, h, 0.185, col, 0.46 + 0.54 * wave)
    return cv.to_rgba()


def _render_comet(
    t_sec: float,
    c1: tuple[float, float, float],
    c2: tuple[float, float, float],
    is_light: bool,
) -> bytes:
    """1x1 Lissajous figure-8 ribbon with dual-tone trail and glowing head."""
    cv = _Canvas(1)
    steps = 22
    for k in range(steps - 1, -1, -1):
        frac = float(k) / float(steps - 1)
        u = t_sec * 3.1 - frac * 2.55
        x = 0.72 * math.sin(u)
        y = 0.62 * math.sin(2.0 * u)
        rad = 0.175 - 0.120 * frac
        alpha = (1.0 - frac) ** 1.3
        col = _mix_rgb(c1, c2, frac * 0.9)
        if k == 0:
            col = _lit_rgb(col, 0.38, is_light)
        cv.stamp_disc(
            x,
            y,
            rad,
            col,
            alpha,
            specular=0.42 if k == 0 else 0.0,
            is_light=is_light,
            glow=0.22 if k == 0 else 0.0,
        )
    return cv.to_rgba()


def _render_frame(spec: SlotSpec, frame_idx: int) -> bytes:
    style_spec = STYLES[spec.style]
    phase = frame_idx / style_spec.frames
    t_sec = phase * style_spec.period
    c1 = _norm(spec.rgb1)
    c2 = _norm(spec.rgb2)
    style = spec.style
    if style == "helix":
        return _render_helix(phase, c1, c2, spec.is_light, spec.cols, spec.inset)
    if style == "gyro":
        return _render_gyro(phase, c1, c2, spec.is_light, spec.cols, spec.inset)
    if style == "octa":
        return _render_octa(phase, c1, c2, spec.is_light)
    if style == "swept":
        return _render_swept(t_sec, c1, c2, spec.is_light)
    if style == "dual":
        return _render_dual(t_sec, c1, c2, spec.is_light, spec.cols, spec.inset)
    if style == "orbit":
        return _render_orbit(t_sec, c1, c2, spec.is_light)
    if style == "wave":
        return _render_wave(t_sec, c1, c2, spec.is_light)
    return _render_comet(t_sec, c1, c2, spec.is_light)


def _kitty_chunks(params: str, raw: bytes, *, cont: str = "q=2") -> str:
    """Encode `raw` as compressed base64 Kitty APC chunks.

    Continuation chunks carry `cont` (e.g. `a=f,q=2` for animation frame data,
    as required by the Kitty Graphics Protocol).
    """
    b64 = base64.b64encode(zlib.compress(raw, level=1)).decode("ascii")
    chunk_size = 4096
    out: list[str] = []
    for i in range(0, len(b64), chunk_size):
        chunk = b64[i : i + chunk_size]
        more = 1 if (i + chunk_size) < len(b64) else 0
        if i == 0:
            out.append(f"\x1b_G{params},m={more};{chunk}\x1b\\")
        else:
            out.append(f"\x1b_G{cont},m={more};{chunk}\x1b\\")
    return "".join(out)


# Room for every slot under both palettes, so switching back re-renders nothing.
@lru_cache(maxsize=2 * len(SLOTS))
def _encode_slot(img_id: int, spec: SlotSpec) -> str:
    style_spec = STYLES[spec.style]
    cols = spec.cols
    w = cols * CELL_W
    h = CELL_H
    gap_ms = max(16, int(round((style_spec.period * 1000.0) / style_spec.frames)))
    # Uppercase `d=I` frees stored image/frame memory, not just placements.
    seq = [f"\x1b_Ga=d,d=I,i={img_id},q=2\x1b\\"]
    for f in range(style_spec.frames):
        raw = _render_frame(spec, f)
        if f == 0:
            seq.append(
                _kitty_chunks(
                    f"a=T,f=32,s={w},v={h},o=z,U=1,c={cols},r=1,i={img_id},q=2",
                    raw,
                )
            )
        else:
            seq.append(
                _kitty_chunks(
                    f"a=f,i={img_id},f=32,s={w},v={h},o=z,z={gap_ms},q=2",
                    raw,
                    cont="a=f,q=2",
                )
            )
    seq.append(f"\x1b_Ga=a,i={img_id},r=1,z={gap_ms},v=1,s=3,q=2\x1b\\")
    return "".join(seq)


def build_upload_payload(slots: Mapping[str, SlotSpec]) -> str:
    """Build the Kitty Graphics APC sequence that uploads and starts looping `slots`."""
    return "".join(_encode_slot(SLOTS[slot], spec) for slot, spec in slots.items())


def delete_payload(slots: Mapping[str, int] | None = None) -> str:
    """Build the Kitty Graphics APC sequence that deletes uploaded spinner images."""
    target = SLOTS if slots is None else slots
    return "".join(f"\x1b_Ga=d,d=I,i={img_id},q=2\x1b\\" for img_id in target.values())


class KittySpinners:
    """Lifecycle and color sync for smooth Kitty Graphics spinners in a session."""

    def __init__(
        self,
        *,
        status_style: str | None,
        tool_style: str | None,
        enabled: bool = True,
    ) -> None:
        self.status_style = status_style
        self.tool_style = tool_style
        self.enabled = enabled and (status_style is not None or tool_style is not None)
        self.ready = False
        self._uploaded_specs: dict[str, SlotSpec] = {}
        self._pending_key: tuple | None = None
        self._ready_payload: tuple[dict[str, SlotSpec], str] | None = None
        self._task: asyncio.Task[Any] | None = None
        self._uploaded_slots: set[str] = set()
        self._slot_cols: dict[str, int] = {}

    @classmethod
    def from_preferences(cls, preferences: Mapping[str, str], *, supported: bool) -> KittySpinners:
        from pcode.preferences import SETTINGS, canonical

        enabled = (
            supported
            and preferences.get("smooth_spinner", SETTINGS["smooth_spinner"].default) == "auto"
            and true_color()
            and not glyphs.active()
        )
        if not enabled:
            return cls(status_style=None, tool_style=None, enabled=False)
        status_cfg = canonical("spinner", preferences.get("spinner", SETTINGS["spinner"].default))
        tool_cfg = canonical(
            "tool_spinner", preferences.get("tool_spinner", SETTINGS["tool_spinner"].default)
        )
        return cls(
            status_style=smooth_style_for("spinner", status_cfg, SETTINGS["spinner"].default),
            tool_style=smooth_style_for("tool_spinner", tool_cfg, SETTINGS["tool_spinner"].default),
            enabled=True,
        )

    def active_for(self, slot: str) -> bool:
        """Whether `slot` is uploaded and ready to render as a Kitty placeholder."""
        return self.enabled and self.ready and slot in self._uploaded_slots

    def _default_cols(self, slot: str) -> int:
        if slot in ("status", "agent") and self.status_style is not None:
            return style_cols(self.status_style)
        if slot in ("startup", "aside", "task"):
            return 2
        return 1

    def slot_cols(self, slot: str) -> int:
        """Cell width of `slot`'s Kitty virtual placement."""
        return self._slot_cols.get(slot, self._default_cols(slot))

    def fragment(self, slot: str) -> tuple[str, str]:
        """Prompt_toolkit fragment for `slot` (1 or more cells wide)."""
        return (f"class:kitty.spinner.{slot}", placeholder_text(self.slot_cols(slot)))

    def _slot_specs(self, chrome_palette, fallback_palette) -> dict[str, SlotSpec]:
        heading_rgb = resolve_rgb(chrome_palette.task_heading, fallback_palette.task_heading)
        muted_rgb = resolve_rgb(chrome_palette.muted, fallback_palette.muted)
        accent_rgb = resolve_rgb(chrome_palette.accent, fallback_palette.accent)
        muted_alt_rgb = _blend_u8(muted_rgb, accent_rgb, 0.35)
        is_light = _is_light_palette(fallback_palette)
        slots: dict[str, SlotSpec] = {}
        if self.status_style is not None:
            slots["status"] = SlotSpec(
                self.status_style,
                style_cols(self.status_style),
                heading_rgb,
                accent_rgb,
                is_light,
            )
            slots["aside"] = SlotSpec("gyro", 2, muted_rgb, muted_alt_rgb, is_light, inset=True)
            slots["startup"] = SlotSpec("gyro", 2, heading_rgb, accent_rgb, is_light)
            # Sub-agents are agents too, so they turn with the main agent's
            # spinner: inset when it is a 2x1 one, to keep their text in place.
            # A wide style with no inset shape gets the 1x1 copy instead.
            wide = style_cols(self.status_style) >= 2 and self.status_style in INSET_STYLES
            slots["agent"] = SlotSpec(
                self.status_style, 2 if wide else 1, heading_rgb, accent_rgb, is_light, wide
            )
        if self.tool_style is not None:
            slots["tool"] = SlotSpec(self.tool_style, 1, muted_rgb, muted_alt_rgb, is_light)
            slots["accent"] = SlotSpec(self.tool_style, 1, accent_rgb, heading_rgb, is_light)
            # Only `dual` has an inset shape; other styles leave task rows on
            # the 1x1 accent slot.
            if self.tool_style == "dual":
                slots["task"] = SlotSpec("dual", 2, accent_rgb, heading_rgb, is_light, inset=True)
        return slots

    def sync(
        self,
        chrome_palette,
        fallback_palette,
        *,
        write_raw: Callable[[str], None],
        flush: Callable[[], None],
        invalidate: Callable[[], None],
        schedule: Callable[[Coroutine[Any, Any, Any]], asyncio.Task[Any]] | None = None,
    ) -> None:
        """Prepare spinner animations on a background thread and flush them on render.

        Called from `PromptLayout.before_render`, so any prepared payload is
        written to the terminal only while prompt_toolkit actively owns the main
        screen — never during `suspended_editor`, external commands, or popups.
        On initial launch, `startup` is prepared and flushed first so the boot
        border animates right away while the remaining slots render behind it.
        """
        if not self.enabled:
            return
        if self._ready_payload is not None:
            ready_specs, payload = self._ready_payload
            self._ready_payload = None
            write_raw(payload)
            flush()
            self._uploaded_specs.update(ready_specs)
            self._uploaded_slots.update(ready_specs)
            for name, spec in ready_specs.items():
                self._slot_cols[name] = spec.cols
            self.ready = True
        slots = self._slot_specs(chrome_palette, fallback_palette)
        if not slots:
            return
        if "startup" in slots and "startup" not in self._uploaded_specs:
            batch = {"startup": slots["startup"]}
        else:
            batch = {
                name: spec for name, spec in slots.items() if self._uploaded_specs.get(name) != spec
            }
        if not batch:
            return
        key = tuple(sorted(batch.items()))
        if key == self._pending_key:
            return
        if schedule is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            schedule = loop.create_task
        self._pending_key = key
        if self._task is not None and not self._task.done():
            self._task.cancel()

        async def _prepare() -> None:
            try:
                payload = await asyncio.to_thread(build_upload_payload, batch)
            except asyncio.CancelledError:
                return
            finally:
                if self._pending_key == key:
                    self._pending_key = None
            if not self.enabled:
                return
            self._ready_payload = (dict(batch), payload)
            invalidate()

        self._task = schedule(_prepare())

    def close(
        self,
        write_raw: Callable[[str], None],
        flush: Callable[[], None],
    ) -> None:
        """Cancel any in-flight upload and delete uploaded images from the terminal."""
        self.enabled = False
        self.ready = False
        self._pending_key = None
        self._ready_payload = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        if self._uploaded_slots:
            to_delete = {slot: SLOTS[slot] for slot in self._uploaded_slots}
            self._uploaded_slots.clear()
            self._uploaded_specs.clear()
            self._slot_cols.clear()
            try:
                write_raw(delete_payload(to_delete))
                flush()
            except OSError:
                pass
