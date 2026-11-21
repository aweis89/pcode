#!/usr/bin/env python3
"""Phase 0 of dev/ghostty-app-plan.md: animate arriving output with Ghostty Smooth's shift.

Throwaway evidence-gathering, not shipped code. Run it inside Ghostty Smooth
(tlpinney/ghostty), in a window of at least 12 rows:

    uv run python dev/smooth_scroll_spike.py

Mimics pcode's screen: a transcript area above a fake editor pinned to the
bottom, output arriving a row at a time, every frame in synchronized output.

The transcript is a smooth-scroll region (tlpinney/ghostty SMOOTH.md, protocol 3):
its last row is an overscan row holding the next line, and the app sets the
sub-cell shift `oy` each frame. The display position springs toward the newest
line on the app's own clock; floor(position) picks the rows, the fraction is oy.

Keys:  m  mode (spring / plain rows)      p  producer (steady / bursty / fast log)
       [/]  steady rate (rows per second) +/-  spring stiffness
       f  frame rate (60 / 120)         s  stalls (off / light / heavy), a busy event loop
       r  region on/off (check the editor never moves either way)     q  quit
"""

import math
import os
import random
import select
import sys
import termios
import time
import tty

EDITOR_ROWS = 5
SNAP_LINES = 8  # backlog above which the display jumps instead of gliding
PRODUCERS = {
    # name: (rows per second while producing, burst length, pause between bursts)
    "steady": (2.0, None, None),  # slow prose; [ ] change it (30 = pcode's code-block rate)
    "bursty": (20.0, 3, 1.5),  # an unpaced model: a few rows, then a gap
    "fast": (400.0, None, None),  # a command log
}
STALLS = {
    # name: (chance a frame stalls, shortest stall, longest stall) in seconds.
    # Stands in for pcode's event loop rendering Markdown or handling tool events.
    "off": (0.0, 0.0, 0.0),
    "light": (0.15, 0.010, 0.030),
    "heavy": (0.25, 0.010, 0.060),
}
WORDS = (
    "the quick brown fox jumps over lazy dog while streaming tokens glide into view "
    "smoothly like neovide and the editor below must never move by a single pixel"
).split()

out = sys.stdout


def w(s: str) -> None:
    out.write(s)


def query_capability(fd: int) -> str:
    w("\x1b_smooth;?\x1b\\")
    out.flush()
    reply = b""
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            reply += os.read(fd, 1024)
            if reply.endswith(b"\x1b\\"):
                break
    return reply.decode(errors="replace").replace("\x1b", "^[") or "no reply"


def spring_step(x: float, v: float, target: float, dt: float, k: float) -> tuple[float, float]:
    """Closed-form critically damped spring: exact for any dt."""
    omega = math.sqrt(k)
    x0 = x - target
    decay = math.exp(-omega * dt)
    c = v + omega * x0
    return target + (x0 + c * dt) * decay, (v - omega * c * dt) * decay


def make_line(n: int, width: int) -> str:
    """One line that never wraps: visible text cut to ``width`` before styling."""
    rng = random.Random(n)
    text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(3, 14)))
    text = text[: max(0, width - 6)]
    color = 31 + n % 6
    return f"\x1b[2m{n:05d}\x1b[0m \x1b[{color}m{text}\x1b[0m"


def main() -> None:
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    cols, rows = os.get_terminal_size()
    region_rows = rows - EDITOR_ROWS - 1  # visible transcript rows; one more is overscan
    if region_rows < 3:
        sys.exit("terminal too small")
    capability = query_capability(fd)

    mode, producer, region_on = "spring", "steady", True
    stiffness, fps = 220.0, 120
    lines: list[str] = []
    pos = vel = 0.0  # display position: index of the top visible line
    produced_at = time.perf_counter()
    owed = 0.0
    burst_left, pause_until = 0, 0.0
    frame_ms: list[float] = []
    snaps = 0
    stall = "off"

    w("\x1b[?25l\x1b[H\x1b[2J")
    try:
        last = time.perf_counter()
        next_frame = last
        while True:
            now = time.perf_counter()
            dt, last = min(now - last, 0.1), now
            frame_ms.append(dt * 1000)
            frame_ms[:] = frame_ms[-fps:]

            # Keys.
            while select.select([fd], [], [], 0)[0]:
                key = os.read(fd, 1).decode(errors="ignore")
                if key == "q":
                    return
                if key == "m":
                    mode = "plain" if mode == "spring" else "spring"
                if key == "p":
                    names = list(PRODUCERS)
                    producer = names[(names.index(producer) + 1) % len(names)]
                    burst_left, pause_until = 0, 0.0
                if key == "+":
                    stiffness *= 1.25
                if key == "-":
                    stiffness /= 1.25
                if key and key in "[]":
                    rate = PRODUCERS["steady"][0] * (1.25 if key == "]" else 0.8)
                    PRODUCERS["steady"] = (max(0.25, min(rate, 400.0)), None, None)
                if key == "f":
                    fps = 60 if fps == 120 else 120
                if key == "s":
                    names = list(STALLS)
                    stall = names[(names.index(stall) + 1) % len(names)]
                if key == "r":
                    region_on = not region_on
                    if not region_on:
                        w("\x1b_smooth;off\x1b\\")

            # Produce lines.
            rate, burst, pause = PRODUCERS[producer]
            if now >= pause_until:
                owed += rate * (now - produced_at)
                while owed >= 1:
                    owed -= 1
                    lines.append(make_line(len(lines), cols - 2))
                    if burst:
                        burst_left += 1
                        if burst_left >= burst:
                            burst_left, owed = 0, 0.0
                            pause_until = now + pause * random.uniform(0.5, 1.5)
                            break
            produced_at = now

            # Move the display.
            target = float(max(0, len(lines) - region_rows))
            if mode == "plain" or not region_on:
                pos, vel = target, 0.0
            else:
                if target - pos > SNAP_LINES:
                    pos, snaps = target - SNAP_LINES, snaps + 1
                pos, vel = spring_step(pos, vel, target, dt, stiffness)
                if abs(target - pos) < 0.001 and abs(vel) < 0.01:
                    pos, vel = target, 0.0
                pos = min(pos, target)
            top = int(pos)
            frac = pos - top
            oy = min(999, int(frac * 1000)) if region_on else 0

            # Draw one synchronized frame.
            w("\x1b[?2026h")
            for row in range(region_rows + 1):  # + overscan row
                i = top + row
                w(f"\x1b[{row + 1};1H\x1b[2K")
                if i < len(lines):
                    w(lines[i])
            if region_on:
                # The shift form makes the right column overscan too; it stays blank.
                w(f"\x1b_smooth;1;0;{region_rows};0;{cols - 1};0;0;0;{oy}\x1b\\")
            avg = sum(frame_ms) / len(frame_ms)
            worst = max(frame_ms)
            p99 = sorted(frame_ms)[int(len(frame_ms) * 0.99) - 1] if len(frame_ms) > 1 else avg
            status = [
                "─" * cols,
                f" mode={mode}  producer={producer}  region={'on' if region_on else 'off'}"
                f"  rate={PRODUCERS[producer][0]:.2f}/s  k={stiffness:.0f}  fps={fps}"
                f"  stalls={stall}",
                f" lines={len(lines)}  lag={target - pos:5.2f}  oy={oy:3d}  snaps={snaps}"
                f"  gap avg={avg:5.2f} p99={p99:5.2f} worst={worst:5.2f}ms",
                f" capability: {capability}",
                " m mode · p producer · [ ] rate · +/- k · f fps · s stalls · r region · q quit",
            ]
            for j, text in enumerate(status):
                w(f"\x1b[{rows - EDITOR_ROWS + 1 + j};1H\x1b[2K\x1b[7m{text[:cols]}\x1b[0m")
            w("\x1b[?2026l")
            out.flush()

            chance, shortest, longest = STALLS[stall]
            if random.random() < chance:
                time.sleep(random.uniform(shortest, longest))

            next_frame += 1 / fps
            delay = next_frame - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_frame = time.perf_counter()
    finally:
        w("\x1b_smooth;off\x1b\\\x1b[?25h\x1b[0m\x1b[H\x1b[2J")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    main()
