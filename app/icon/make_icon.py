#!/usr/bin/env python3
"""Regenerate the Pcode.app icon from pcode's default "helix" spinner.

Stdlib only (PNG written with zlib); `iconutil` (macOS) builds the .icns.

    python3 make_icon.py                       # icon_1024.png + AppIcon.icns here
    python3 make_icon.py --png-only            # just icon_1024.png
    python3 make_icon.py --preview 32 out.png [phase]  # one PNG at any size

The drawing reproduces `_render_helix` (2x1, five pairs) and `_Canvas.stamp_disc`
from pcode's `src/pcode/kitty_spinner.py` as vector SDFs, so every icon size is
rendered natively rather than downscaled. Strand colors match the status slot
on the dark palette: strand 1 is `task_heading` (#c4b5fd), strand 2 is
`accent` (#88c0d0).

Tile follows the macOS grid: 1024 canvas, 824x824 continuous-corner rounded
rect (corner radius ~185) centred with a transparent margin and a faint shadow.
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

TAU = 2.0 * math.pi
HERE = os.path.dirname(os.path.abspath(__file__))

# Status helix colors from pcode's dark palette (ui.PALETTES["dark"]).
C1 = (0xC4 / 255, 0xB5 / 255, 0xFD / 255)  # task_heading
C2 = (0x88 / 255, 0xC0 / 255, 0xD0 / 255)  # accent
BG = (0.0, 0.0, 0.0)

# Chosen frames: phase in [0, 1), as in `frame_idx / 84`. Both put the
# lavender strand in front on the left and the cyan one in front on the right.
PHASE = 0.125
COMPACT_PHASE = 0.0

# Helix geometry from `_render_helix`, in half-cell units:
# (n_pairs, span, amp, phase_step, r_back, r_span).
WIDE = (5, 1.54, 0.84, 0.78, 0.16, 0.135)  # the 2x1 status spinner
COMPACT = (3, 0.62, 0.78, 1.05, 0.13, 0.10)  # its 1x1 form, for tiny sizes
# Icon sizes at or below this use COMPACT, as 10 beads turn to dust at 32px.
COMPACT_MAX = 32
# Beads are drawn this much larger than in the terminal so they hold up as an
# icon; the terminal glyph has a whole row of text to sit beside.
BEAD_SCALE = 1.2

# Tile geometry on the 1024 grid.
TILE = 824.0
CORNER = 185.0
# Largest extent of the helix (outer bead edge to edge) as a fraction of the tile.
HELIX_FRAC = 0.66


def smoothstep(e0: float, e1: float, x: float) -> float:
    t = (x - e0) / (e1 - e0)
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return t * t * (3.0 - 2.0 * t)


def mix(a, b, t):
    it = 1.0 - t
    return (a[0] * it + b[0] * t, a[1] * it + b[1] * t, a[2] * it + b[2] * t)


class Canvas:
    """Premultiplied RGBA float canvas."""

    def __init__(self, size: int) -> None:
        self.size = size
        n = size * size
        self.r = [0.0] * n
        self.g = [0.0] * n
        self.b = [0.0] * n
        self.a = [0.0] * n

    def over(self, idx: int, rgb, a: float) -> None:
        inv = 1.0 - a
        self.r[idx] = rgb[0] * a + self.r[idx] * inv
        self.g[idx] = rgb[1] * a + self.g[idx] * inv
        self.b[idx] = rgb[2] * a + self.b[idx] * inv
        self.a[idx] = a + self.a[idx] * inv

    def png(self) -> bytes:
        s = self.size
        raw = bytearray()
        for y in range(s):
            raw.append(0)
            for x in range(s):
                i = y * s + x
                a = self.a[i]
                if a <= 1e-4:
                    raw += b"\0\0\0\0"
                    continue
                inv = 1.0 / a
                raw += bytes(
                    min(255, int(c * inv * 255.0 + 0.5)) for c in (self.r[i], self.g[i], self.b[i])
                )
                raw.append(min(255, int(a * 255.0 + 0.5)))

        def chunk(tag: bytes, data: bytes) -> bytes:
            body = tag + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

        ihdr = struct.pack(">IIBBBBB", s, s, 8, 6, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b"")
        )


def squircle_sd(px: float, py: float, half: float, corner: float) -> float:
    """Approximate signed distance (pixels) to a continuous-corner rounded square.

    Corners are superellipse quadrants spanning 1.528x the nominal radius, the
    usual construction for Apple's smooth corners; the exponent is chosen so
    the diagonal inset equals that of a circular radius of `corner`.
    """
    ext = corner * 1.528
    n = math.log(2.0) / -math.log(1.0 - corner * (1.0 - 1.0 / math.sqrt(2.0)) / ext)
    ax, ay = abs(px), abs(py)
    inner = half - ext
    if ax <= inner or ay <= inner:
        return max(ax, ay) - half
    dx = (ax - inner) / ext
    dy = (ay - inner) / ext
    f = (dx**n + dy**n) ** (1.0 / n)
    # First-order distance: (f - 1) / |grad f|, in pixels.
    gx = dx ** (n - 1.0) * f ** (1.0 - n)
    gy = dy ** (n - 1.0) * f ** (1.0 - n)
    g = math.hypot(gx, gy) or 1.0
    return (f - 1.0) * ext / g


def draw_tile(cv: Canvas) -> None:
    s = cv.size
    k = s / 1024.0
    half = TILE * 0.5 * k
    corner = CORNER * k
    c = s * 0.5
    aa = 0.75
    shadow_dy, shadow_blur, shadow_a = 6.0 * k, 18.0 * k + 1.0, 0.28
    for y in range(s):
        py = y + 0.5 - c
        for x in range(s):
            px = x + 0.5 - c
            idx = y * s + x
            d_tile = squircle_sd(px, py, half, corner)
            if d_tile > -aa:
                d_sh = squircle_sd(px, py - shadow_dy, half, corner)
                sa = shadow_a * (1.0 - smoothstep(-shadow_blur * 0.5, shadow_blur, d_sh))
                if sa > 0.0:
                    cv.over(idx, (0.0, 0.0, 0.0), sa)
            a = smoothstep(aa, -aa, d_tile)
            if a > 0.0:
                cv.over(idx, BG, a)


def stamp_disc(cv, cx, cy, scale, x0, y0, radius, rgb, alpha, specular, glow, aa) -> None:
    """Port of `_Canvas.stamp_disc` (dark theme) at arbitrary scale."""
    s = cv.size
    reach = radius * (1.85 if glow > 0.0 else 1.0) + aa
    ix0 = max(0, int(cx + (x0 - reach) * scale))
    ix1 = min(s, int(cx + (x0 + reach) * scale) + 2)
    iy0 = max(0, int(cy + (y0 - reach) * scale))
    iy1 = min(s, int(cy + (y0 + reach) * scale) + 2)
    inv_s = 1.0 / scale
    spec_x = x0 - radius * 0.28
    spec_y = y0 - radius * 0.28
    spec_r = max(radius * 0.55, 0.05)
    for iy in range(iy0, iy1):
        py = (iy + 0.5 - cy) * inv_s
        dy2 = (py - y0) ** 2
        for ix in range(ix0, ix1):
            px = (ix + 0.5 - cx) * inv_s
            dist = math.sqrt((px - x0) ** 2 + dy2)
            d = dist - radius
            a = smoothstep(aa, -aa, d) * alpha
            if glow > 0.0 and dist < reach:
                t = 1.0 - dist / reach
                ga = t * t * glow * alpha
                if ga > a:
                    a = ga
            if a <= 0.002:
                continue
            col = rgb
            if specular > 0.0 and d < aa:
                sd = math.hypot(px - spec_x, py - spec_y) / spec_r
                sp = max(0.0, 1.0 - sd)
                sp = sp * sp * specular
                if sp > 0.005:
                    col = mix(rgb, (1.0, 1.0, 1.0), sp)
            cv.over(iy * s + ix, col, a)


def draw_helix(cv: Canvas, phase: float) -> None:
    s = cv.size
    n_pairs, span, amp, phase_step, r_back, r_span = COMPACT if s <= COMPACT_MAX else WIDE
    r_back *= BEAD_SCALE
    r_span *= BEAD_SCALE
    extent = 2.0 * (max(span, amp) + r_back + r_span)
    scale = HELIX_FRAC * TILE * (s / 1024.0) / extent
    cx = cy = s * 0.5
    # About one output pixel of anti-aliasing at every size.
    aa = 0.8 / scale

    prims = []
    for k in range(n_pairs):
        x = -span + 2.0 * span * k / (n_pairs - 1)
        phi = phase * TAU - k * phase_step
        ya = amp * math.sin(phi)
        za = math.cos(phi)
        for y, z, col in ((ya, za, C1), (-ya, -za, C2)):
            depth = 0.5 + 0.5 * z
            rad = r_back + r_span * depth
            alpha = 0.24 + 0.76 * depth**1.15
            spec = 0.45 * depth**1.4
            prims.append((z, (x, y, rad, col, alpha, spec, 0.26 * depth)))
    prims.sort(key=lambda p: p[0])
    for _, (x, y, rad, col, alpha, spec, gl) in prims:
        stamp_disc(cv, cx, cy, scale, x, y, rad, col, alpha, spec, gl, aa)


def render(size: int, phase: float | None = None) -> bytes:
    if phase is None:
        phase = COMPACT_PHASE if size <= COMPACT_MAX else PHASE
    cv = Canvas(size)
    draw_tile(cv)
    draw_helix(cv, phase)
    return cv.png()


def write(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def build_icns(out: str) -> None:
    tmp = tempfile.mkdtemp()
    iconset = os.path.join(tmp, "AppIcon.iconset")
    os.mkdir(iconset)
    try:
        cache: dict[int, bytes] = {}
        for base in (16, 32, 128, 256, 512):
            for mult in (1, 2):
                px = base * mult
                if px not in cache:
                    cache[px] = render(px)
                suffix = "" if mult == 1 else "@2x"
                write(os.path.join(iconset, f"icon_{base}x{base}{suffix}.png"), cache[px])
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out], check=True)
    finally:
        shutil.rmtree(tmp)


def main(argv: list[str]) -> None:
    if argv[:1] == ["--preview"]:
        phase = float(argv[3]) if len(argv) > 3 else None
        write(argv[2], render(int(argv[1]), phase))
        return
    write(os.path.join(HERE, "icon_1024.png"), render(1024))
    if "--png-only" not in argv:
        build_icns(os.path.join(HERE, "AppIcon.icns"))


if __name__ == "__main__":
    main(sys.argv[1:])
