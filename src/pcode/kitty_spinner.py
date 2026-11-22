"""Smooth terminal-clocked spinners via Kitty Graphics Protocol animations.

Uploads tiny anti-aliased RGBA animation frames once and places them inline as
1-cell Unicode placeholders (`U+10EEEE`), with the image ID encoded in the
cell's 24-bit foreground color (`fg:#rrggbb`). Ghostty and kitty advance the
frames on their own clock, so animations stay fluid while Python repaints far
less often. Terminals without Kitty graphics, multiplexers (`TMUX`/`STY`),
non-truecolor modes, ASCII glyph mode, or `smooth_spinner = off` fall back to
Rich's text spinners transparently.
"""

from __future__ import annotations

import asyncio
import base64
import math
import os
import re
import zlib
from collections.abc import Callable, Coroutine, Mapping
from functools import cache
from typing import Any

from pcode import glyphs
from pcode.terminal_color import true_color

TAU = 2.0 * math.pi
PLACEHOLDER = "\U0010eeee"

# Standard Kitty graphics query (id 31 does not store an image). Sent ahead of
# OSC 11 during startup detection so a terminal that speaks Kitty graphics
# replies `\x1b_Gi=31;OK\x1b\\` before the background answer ends the wait.
QUERY = b"\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\"
REPLY = re.compile(rb"\x1b_Gi=31;([^\x07\x1b]{0,64})\x1b\\")

# Cell image dimensions (1:2 aspect ratio matches typical monospace cells).
CELL_W = 28
CELL_H = 56

# Smooth spinner styles: name -> (period_seconds, frame_count, rich_fallback).
STYLES: dict[str, tuple[float, int, str]] = {
    "swept": (TAU / 2.4, 110, "dots"),
    "orbit": (TAU / 3.9, 72, "dots"),
    "diamond": (TAU / 1.9, 120, "dots"),
    "dual": (TAU / 1.8, 126, "arc"),
    "wave": (TAU / 5.4, 54, "dots"),
    "comet": (TAU / 3.1, 90, "dots"),
}
SMOOTH_NAMES: tuple[str, ...] = tuple(STYLES)
DEFAULT_STYLE: dict[str, str] = {"spinner": "swept", "tool_spinner": "dual"}

# High 24-bit image IDs ("pc\x01".."pc\x05") so they never collide with user
# images displayed in the same terminal.
SLOTS: dict[str, int] = {
    "status": 0x706301,
    "tool": 0x706302,
    "accent": 0x706303,
    "aside": 0x706304,
    "startup": 0x706305,
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
    echo unknown APC sequences.
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
    return spec[2] if spec is not None else name


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


@cache
def _grid() -> tuple[int, int, tuple[tuple[float, float, float, float], ...], float]:
    s = float(min(CELL_W, CELL_H))
    cx = 0.5 * CELL_W
    cy = 0.52 * CELL_H
    half_s = 0.5 * s
    aa = 2.0 / s
    y_min = max(0, int(cy - half_s * 1.25))
    y_max = min(CELL_H, int(cy + half_s * 1.25) + 1)
    coords: list[tuple[float, float, float, float]] = []
    for y in range(y_min, y_max):
        py = (y + 0.5 - cy) / half_s
        for x in range(CELL_W):
            px = (x + 0.5 - cx) / half_s
            coords.append((px, py, math.hypot(px, py), math.atan2(py, px)))
    return y_min, y_max, tuple(coords), aa


def _render_alpha_band(style: str, t: float) -> bytes:
    """Render one frame's 8-bit alpha mask for the active vertical band."""
    y_min, y_max, coords, aa = _grid()
    buf = bytearray((y_max - y_min) * CELL_W)

    if style == "swept":
        head = t * 4.8 + 0.75 * math.sin(t * 2.4)
        sweep = 2.25 + 1.15 * math.sin(t * 2.4 - 1.0)
        r, w = 0.74, 0.135
        for i, (px, py, dist, ang) in enumerate(coords):
            d, frac = _sd_arc_cap(px, py, dist, ang, r, w, head, sweep)
            arc = _smoothstep(aa, -aa, d) * (1.0 - 0.82 * frac)
            track = _smoothstep(aa, -aa, abs(dist - r) - w * 0.65) * 0.14
            a = arc if arc > track else track
            if a > 0.002:
                buf[i] = int(min(1.0, a) * 255.0 + 0.5)

    elif style == "orbit":
        r = 0.68
        dots = []
        for k in range(4):
            fk = float(k)
            base = t * 3.9 - fk * 0.48
            ang = base + 0.42 * math.sin(base)
            dots.append((
                math.cos(ang) * r,
                math.sin(ang) * r,
                0.22 - (0.11 * fk / 3.0),
                1.0 - (0.66 * fk / 3.0),
            ))
        for i, (px, py, _, _) in enumerate(coords):
            a = 0.0
            for dx, dy, dr, da in dots:
                val = _smoothstep(aa, -aa, math.hypot(px - dx, py - dy) - dr) * da
                if val > a:
                    a = val
            if a > 0.002:
                buf[i] = int(min(1.0, a) * 255.0 + 0.5)

    elif style == "diamond":
        pulse = 0.5 + 0.5 * math.sin(t * 3.8)
        r_in = 0.34 + 0.16 * pulse
        core_gain = 0.70 + 0.30 * pulse
        r_out = 0.76 + 0.12 * pulse
        rot = t * 1.9
        cs, sn = math.cos(rot), math.sin(rot)
        inv_s2 = 0.70710678
        for i, (px, py, _, _) in enumerate(coords):
            d_core = (abs(px) + abs(py)) * inv_s2 - r_in * inv_s2
            core = _smoothstep(aa, -aa, d_core) * core_gain
            qx = cs * px - sn * py
            qy = sn * px + cs * py
            d_ring = abs((abs(qx) + abs(qy)) * inv_s2 - r_out * inv_s2) - 0.075
            c2 = abs(math.cos(math.atan2(qy, qx) * 2.0))
            corner_glow = 0.45 + 0.55 * (c2 * c2)
            halo = _smoothstep(aa, -aa, d_ring) * corner_glow * 0.85
            a = core if core > halo else halo
            if a > 0.002:
                buf[i] = int(min(1.0, a) * 255.0 + 0.5)

    elif style == "dual":
        h1 = t * 3.6
        h2 = -t * 5.4
        for i, (px, py, dist, ang) in enumerate(coords):
            d1, f1 = _sd_arc_cap(px, py, dist, ang, 0.78, 0.095, h1, 2.5)
            d2, f2 = _sd_arc_cap(px, py, dist, ang, 0.43, 0.090, h2, 2.3)
            a1 = _smoothstep(aa, -aa, d1) * (1.0 - 0.72 * f1)
            a2 = _smoothstep(aa, -aa, d2) * (0.82 - 0.60 * f2)
            a = a1 if a1 > a2 else a2
            if a > 0.002:
                buf[i] = int(min(1.0, a) * 255.0 + 0.5)

    elif style == "wave":
        bars = []
        for k in range(3):
            cx = (float(k) - 1.0) * 0.58
            wave = 0.5 + 0.5 * math.sin(t * 5.4 - float(k) * 1.15)
            h = 0.18 + 0.70 * wave
            bars.append((cx, h, 0.45 + 0.55 * wave))
        for i, (px, py, _, _) in enumerate(coords):
            a = 0.0
            for cx, h, gain in bars:
                cy_clamped = -h if py < -h else (h if py > h else py)
                d = math.hypot(px - cx, py - cy_clamped) - 0.165
                val = _smoothstep(aa, -aa, d) * gain
                if val > a:
                    a = val
            if a > 0.002:
                buf[i] = int(min(1.0, a) * 255.0 + 0.5)

    else:  # comet
        steps = 20
        pts = []
        for k in range(steps):
            frac = float(k) / float(steps - 1)
            u = t * 3.1 - frac * 2.55
            pts.append((
                0.76 * math.sin(u),
                0.52 * math.sin(2.0 * u),
                0.19 - 0.135 * frac,
                (1.0 - frac) ** 1.35,
            ))
        for i, (px, py, _, _) in enumerate(coords):
            a = 0.0
            for dx, dy, dr, da in pts:
                val = _smoothstep(aa, -aa, math.hypot(px - dx, py - dy) - dr) * da
                if val > a:
                    a = val
            if a > 0.002:
                buf[i] = int(min(1.0, a) * 255.0 + 0.5)

    return bytes(buf)


@cache
def _alpha_frames(style: str) -> tuple[int, tuple[bytes, ...]]:
    """Pre-rendered alpha masks for `style`, reused across color/theme changes."""
    period, n_frames, _ = STYLES[style]
    gap_ms = max(16, int(round((period * 1000.0) / n_frames)))
    frames = tuple(_render_alpha_band(style, (f / n_frames) * period) for f in range(n_frames))
    return gap_ms, frames


def _tint_rgba(alpha_band: bytes, rgb: tuple[int, int, int]) -> bytes:
    """Expand an 8-bit alpha band into a full CELL_W x CELL_H RGBA frame."""
    y_min, y_max, _, _ = _grid()
    top_pad = b"\x00" * (y_min * CELL_W * 4)
    bot_pad = b"\x00" * ((CELL_H - y_max) * CELL_W * 4)
    r, g, b = rgb
    buf = bytearray(len(alpha_band) * 4)
    idx = 0
    for a in alpha_band:
        if a:
            buf[idx] = r
            buf[idx + 1] = g
            buf[idx + 2] = b
            buf[idx + 3] = a
        idx += 4
    return top_pad + bytes(buf) + bot_pad


def _kitty_chunks(params: str, raw: bytes) -> str:
    b64 = base64.b64encode(zlib.compress(raw, level=1)).decode("ascii")
    chunk_size = 4096
    out: list[str] = []
    for i in range(0, len(b64), chunk_size):
        chunk = b64[i : i + chunk_size]
        more = 1 if (i + chunk_size) < len(b64) else 0
        if i == 0:
            out.append(f"\x1b_G{params},m={more};{chunk}\x1b\\")
        else:
            out.append(f"\x1b_Gm={more},q=2;{chunk}\x1b\\")
    return "".join(out)


def build_upload_payload(slots: Mapping[str, tuple[str, tuple[int, int, int]]]) -> str:
    """Build the Kitty Graphics APC sequence that uploads and starts looping `slots`."""
    seq: list[str] = []
    for slot, (style, rgb) in slots.items():
        img_id = SLOTS[slot]
        gap_ms, frames = _alpha_frames(style)
        # Uppercase `d=I` frees stored image/frame memory, not just placements.
        seq.append(f"\x1b_Ga=d,d=I,i={img_id},q=2\x1b\\")
        for f, alpha_band in enumerate(frames):
            raw = _tint_rgba(alpha_band, rgb)
            if f == 0:
                seq.append(
                    _kitty_chunks(
                        f"a=T,f=32,s={CELL_W},v={CELL_H},o=z,U=1,c=1,r=1,i={img_id},q=2",
                        raw,
                    )
                )
            else:
                seq.append(
                    _kitty_chunks(
                        f"a=f,i={img_id},f=32,s={CELL_W},v={CELL_H},o=z,z={gap_ms},q=2",
                        raw,
                    )
                )
        seq.append(f"\x1b_Ga=a,i={img_id},r=1,z={gap_ms},v=1,s=3,q=2\x1b\\")
    return "".join(seq)


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
        self._uploaded_key: tuple | None = None
        self._pending_key: tuple | None = None
        self._ready_payload: tuple[tuple, set[str], str] | None = None
        self._task: asyncio.Task[Any] | None = None
        self._uploaded_slots: set[str] = set()

    @classmethod
    def from_preferences(
        cls, preferences: Mapping[str, str], *, supported: bool
    ) -> KittySpinners:
        from pcode.preferences import SETTINGS

        enabled = (
            supported
            and preferences.get("smooth_spinner", SETTINGS["smooth_spinner"].default) == "auto"
            and true_color()
            and not glyphs.active()
        )
        if not enabled:
            return cls(status_style=None, tool_style=None, enabled=False)
        status_cfg = preferences.get("spinner", SETTINGS["spinner"].default)
        tool_cfg = preferences.get("tool_spinner", SETTINGS["tool_spinner"].default)
        return cls(
            status_style=smooth_style_for("spinner", status_cfg, SETTINGS["spinner"].default),
            tool_style=smooth_style_for("tool_spinner", tool_cfg, SETTINGS["tool_spinner"].default),
            enabled=True,
        )

    def active_for(self, slot: str) -> bool:
        """Whether `slot` is uploaded and ready to render as a Kitty placeholder."""
        return self.enabled and self.ready and slot in self._uploaded_slots

    def fragment(self, slot: str) -> tuple[str, str]:
        """1-cell prompt_toolkit fragment for `slot`."""
        return (f"class:kitty.spinner.{slot}", PLACEHOLDER)

    def _slot_specs(
        self, chrome_palette, fallback_palette
    ) -> dict[str, tuple[str, tuple[int, int, int]]]:
        heading_rgb = resolve_rgb(chrome_palette.task_heading, fallback_palette.task_heading)
        muted_rgb = resolve_rgb(chrome_palette.muted, fallback_palette.muted)
        accent_rgb = resolve_rgb(chrome_palette.accent, fallback_palette.accent)
        slots: dict[str, tuple[str, tuple[int, int, int]]] = {}
        if self.status_style is not None:
            slots["status"] = (self.status_style, heading_rgb)
            slots["aside"] = (self.status_style, muted_rgb)
            slots["startup"] = ("diamond", heading_rgb)
        if self.tool_style is not None:
            slots["tool"] = (self.tool_style, muted_rgb)
            slots["accent"] = (self.tool_style, accent_rgb)
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
        """
        if not self.enabled:
            return
        if self._ready_payload is not None:
            ready_key, ready_slots, payload = self._ready_payload
            self._ready_payload = None
            write_raw(payload)
            flush()
            self._uploaded_key = ready_key
            self._uploaded_slots = ready_slots
            self.ready = True
        slots = self._slot_specs(chrome_palette, fallback_palette)
        if not slots:
            return
        key = tuple(sorted(slots.items()))
        if key == self._uploaded_key or key == self._pending_key:
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
                payload = await asyncio.to_thread(build_upload_payload, slots)
            except asyncio.CancelledError:
                return
            finally:
                if self._pending_key == key:
                    self._pending_key = None
            if not self.enabled:
                return
            self._ready_payload = (key, set(slots), payload)
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
            self._uploaded_key = None
            try:
                write_raw(delete_payload(to_delete))
                flush()
            except OSError:
                pass
