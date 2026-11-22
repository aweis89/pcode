"""Compare native (Ghostty Smooth) pacing with the Python pacer on one scripted reply.

    uv run --no-sync python dev/pacing_bench.py                  # typical, 4 setups x 5 runs
    uv run --no-sync python dev/pacing_bench.py --profile fast --spinners kitty
    uv run --no-sync python dev/pacing_bench.py --src .worktrees/base   # before/after a change

Each run opens a fresh Ghostty Smooth window (your Ghostty config: with the
default config the window does not answer the smooth probe in time) that
streams a scripted mixed reply (prose, lists, code, tables) through the real
interactive app with `paced_scrollback typed`. Setups cross `smooth_scroll`
(native = `auto`, python = `off`) with `smooth_spinner` (kitty = `auto`,
text = `off`), run in ABBA order so drift and background load hit all.

Profiles: `typical` thinks for 10 s first (only the spinner moves; measured
separately as renders/s and CPU %), then streams ~40 tokens/s with pauses
between sections (~140 lines). `fast` is one ~180 tokens/s burst (~400 lines),
where streamed text dominates redraws.

Per run it records, from the first streamed delta to the last handoff: editor
renders and redraw requests by source, handoffs and the time spent in them,
pcode CPU, Ghostty CPU, and pcode event-loop lag (a 5 ms heartbeat). Native
runs also record Ghostty's draw-to-draw frame gaps while the glide animates,
via the fork's `GHOSTTY_SMOOTH_FRAME_LOG`, pooled across runs. Ghostty stops
drawing hidden windows, so frame gaps need the benchmark windows on screen.

A run that glided when it shouldn't (or didn't when it should), or whose
spinners were the wrong kind, measured the wrong setup; the script prints what
it has and stops. Results are noisy (roughly +/-20% run to run): compare
averages over several runs. With `typical`, each run takes about a minute.
"""

import argparse
import asyncio
import json
import os
import random
import resource
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from bisect import bisect_right
from contextlib import asynccontextmanager
from pathlib import Path

MODES = {"native": "auto", "python": "off"}
SPINNERS = {"kitty": "auto", "text": "off"}
# How the scripted model streams. `fast` is a burst with no pauses, so text
# dominates redraws; `typical` thinks first (only the spinner moves), streams
# at roughly 40 tokens/s, and pauses between sections as tool calls would.
PROFILES = {
    "fast": {"chunk": 12, "delta_s": 1 / 60, "sections": 14, "idle_s": 0, "pause_s": 0},
    "typical": {"chunk": 6, "delta_s": 1 / 25, "sections": 5, "idle_s": 10, "pause_s": 1.5},
}
SETTLE_S = 3  # pacing has drained once handoffs stop for this long
BUCKET_MS = 0.25  # the fork's frame-log histogram resolution


def reply(count: int) -> list[str]:
    rng = random.Random(7)
    sections = []
    for i in range(count):
        prose = " ".join(f"word{j}" for j in range(rng.randint(25, 60)))
        code = "\n".join(f"    value_{k} = compute({k}, {i})  # step {k}" for k in range(8))
        sections.append(
            f"## Section {i}\n\n{prose}\n\n"
            f"- first point for section {i}\n- second point with a bit more text\n- third\n\n"
            f"```python\ndef fn_{i}():\n{code}\n    return value_0\n```\n\n"
            f"| col a | col b | col c |\n|---|---|---|\n| {i} | x | y |\n| {i + 1} | z | w |\n\n"
        )
    sections[-1] += "BENCH DONE\n"
    return sections


def _cpu_of(pid: int) -> float | None:
    out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True)
    text = out.stdout.strip()
    if not text:
        return None
    days, _, clock = text.rpartition("-")
    seconds = 0.0
    for part in clock.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds + 86400 * int(days or 0)


def _ghostty_pid() -> int | None:
    pid = os.getpid()
    while pid > 1:
        command = ["ps", "-o", "ppid=,comm=", "-p", str(pid)]
        fields = subprocess.run(command, capture_output=True, text=True).stdout.split(None, 1)
        if len(fields) < 2:
            return None
        if "ghostty" in fields[1].lower():
            return pid
        pid = int(fields[0])
    return None


def _pct(values: list[float], q: float) -> float:
    return values[min(len(values) - 1, int(len(values) * q))] if values else 0.0


def _redraw_source() -> str:
    """The innermost pcode frame that asked for a redraw, as `module.function`.

    A callback the event loop ran (prompt_toolkit's own timers, CPR replies)
    has only the loop and pcode's entry point above it: it counts as
    `prompt_toolkit`, not as that entry point.
    """
    frame = sys._getframe(2)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module.startswith("asyncio"):
            break
        if module.startswith("pcode."):
            return f"{module.removeprefix('pcode.')}.{frame.f_code.co_name}"
        frame = frame.f_back
    return "prompt_toolkit"


def child(out: Path, frames: Path, profile: dict) -> None:
    """Run inside the measured terminal: stream the reply, write stats to ``out``."""
    from collections import Counter

    from prompt_toolkit.application import Application
    from prompt_toolkit.renderer import Renderer
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel

    import pcode.ui as ui
    from pcode import theme
    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime

    # Who asks for redraws, and how many renders actually happen. prompt_toolkit
    # coalesces invalidations, so renders are what cost CPU.
    redraws: Counter[str] = Counter()
    counting = {"on": False, "renders": 0}
    invalidate, render = Application.invalidate, Renderer.render

    def counting_invalidate(self):
        if counting["on"]:
            redraws[_redraw_source()] += 1
        return invalidate(self)

    def counting_render(self, *args, **kwargs):
        if counting["on"]:
            counting["renders"] += 1
        return render(self, *args, **kwargs)

    Application.invalidate = counting_invalidate
    Renderer.render = counting_render

    stats = {"handoffs": 0, "handoff_ms": [], "gliding": False, "last": 0.0, "last_cpu": 0.0}
    lags: list[tuple[float, float]] = []  # (time, lag ms)
    ghostty_cpu: list[tuple[float, float]] = []  # (time, seconds), sampled by a thread
    mark: dict[str, float] = {}
    ghostty = _ghostty_pid()

    def cpu() -> float:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_utime + usage.ru_stime

    # Streaming handoffs look `suspended_editor` up through `pcode.ui`, so this
    # counts them; `pcode.app` imported it by name (popups), which is not counted.
    original = ui.suspended_editor

    @asynccontextmanager
    async def timed(app, **kwargs):
        start = time.perf_counter()
        async with original(app, **kwargs) as handoff:
            yield handoff
        end = time.perf_counter()
        stats["handoffs"] += 1
        stats["handoff_ms"].append((end - start) * 1000)
        stats["last"], stats["last_cpu"] = end, cpu()

    ui.suspended_editor = timed
    flush = ui.TerminalOutput.flush

    async def watching_flush(self, **kwargs):
        stats["gliding"] = stats["gliding"] or self.gliding
        return await flush(self, **kwargs)

    ui.TerminalOutput.flush = watching_flush

    async def heartbeat():
        while True:
            start = time.perf_counter()
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            lags.append((now, (now - start - 0.005) * 1000))

    def sample_ghostty():
        while ghostty:
            seconds = _cpu_of(ghostty)
            if seconds is not None:
                ghostty_cpu.append((time.perf_counter(), seconds))
            time.sleep(0.25)

    def ghostty_at(t: float) -> float | None:
        times = [s[0] for s in ghostty_cpu]
        i = bisect_right(times, t)
        return ghostty_cpu[i - 1][1] if i else None

    def finish():
        while True:
            seen = stats["handoffs"]
            time.sleep(SETTLE_S)
            if stats["handoffs"] == seen:
                break
        counting["on"] = False  # includes up to SETTLE_S of idle; both modes alike
        # Measure up to the last handoff, not through the idle settle wait.
        end = stats["last"] or time.perf_counter()
        time.sleep(0.3)  # one more Ghostty sample past `end`
        handoff_ms = sorted(stats["handoff_ms"])
        lag = sorted(ms for t, ms in lags if t <= end)
        g_end = ghostty_at(end)
        result = {
            "gliding": stats["gliding"],
            "probe": theme._glide_protocol,
            "pcode_src": str(Path(ui.__file__).parent.parent),
            "handoffs": stats["handoffs"],
            "handoff_ms_total": round(sum(handoff_ms), 1),
            "handoff_ms_p50": round(_pct(handoff_ms, 0.5), 2),
            "handoff_ms_max": round(handoff_ms[-1], 2) if handoff_ms else 0.0,
            "wall_s": round(end - mark["t"], 2),
            "pcode_cpu_s": round((stats["last_cpu"] or cpu()) - mark["cpu"], 2),
            "ghostty_cpu_s": (
                round(g_end - mark["ghostty"], 2)
                if g_end is not None and mark["ghostty"] is not None
                else None
            ),
            "loop_lag_ms_p99": round(_pct(lag, 0.99), 2),
            "loop_lag_ms_max": round(lag[-1], 2) if lag else 0.0,
            "beats_over_16ms": sum(1 for x in lag if x > 16),
            **idle,
            # The status slot itself, not just the startup upload.
            "kitty_spinners": bool(spinners and spinners.active_for("status")),
            "renders": counting["renders"],
            "invalidations": sum(redraws.values()),
            "redraw_sources": dict(redraws.most_common(8)),
            "frame_log_from": mark["frames"],
            # Monotonic ns, comparable with the frame log's clock on macOS.
            "start_ns": int(mark["t"] * 1e9),
            "end_ns": int(end * 1e9),
        }
        out.write_text(json.dumps(result))
        os._exit(0)

    sections = reply(profile["sections"])
    chunk, delta_s = profile["chunk"], profile["delta_s"]
    tasks = set()
    idle: dict = {}

    async def model(messages, info):
        threading.Thread(target=sample_ghostty, daemon=True).start()
        await asyncio.sleep(1.5)  # let the startup probe and layout settle
        if profile["idle_s"]:
            # Thinking: nothing streams, so only the spinner and status redraw.
            redraws.clear()
            counting.update(on=True, renders=0)
            start, start_cpu = time.perf_counter(), cpu()
            start_ghostty = ghostty_at(start)
            await asyncio.sleep(profile["idle_s"])
            now = time.perf_counter()
            seconds = now - start
            end_ghostty = ghostty_at(now)
            idle.update(
                idle_renders_per_s=round(counting["renders"] / seconds, 2),
                idle_redraws_per_s=round(sum(redraws.values()) / seconds, 2),
                idle_pcode_cpu_pct=round(100 * (cpu() - start_cpu) / seconds, 1),
                idle_ghostty_cpu_pct=(
                    round(100 * (end_ghostty - start_ghostty) / seconds, 1)
                    if end_ghostty is not None and start_ghostty is not None
                    else None
                ),
                idle_redraw_sources=dict(redraws.most_common(5)),
            )
        # Started after thinking, so its wakeups are not in the idle CPU.
        tasks.add(asyncio.get_running_loop().create_task(heartbeat()))
        lags.clear()
        redraws.clear()
        counting.update(on=True, renders=0)
        stats.update(handoffs=0, handoff_ms=[], last=0.0, last_cpu=0.0)
        mark.update(
            t=time.perf_counter(),
            cpu=cpu(),
            ghostty=ghostty_at(time.perf_counter()),
            # Frame-log lines from startup animations are not part of the run.
            frames=frames.stat().st_size if frames.exists() else 0,
        )
        for n, text in enumerate(sections):
            if n and profile["pause_s"]:
                await asyncio.sleep(profile["pause_s"])
            for i in range(0, len(text), chunk):
                yield text[i : i + chunk]
                await asyncio.sleep(delta_s)
        threading.Thread(target=finish, daemon=True).start()

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    app = PreviewApp(model="test:local", runtime=runtime, initial_prompt="bench")
    spinners = None

    def current_spinners():
        nonlocal spinners
        # Built once the prompt starts; read late so `finish` sees it.
        while spinners is None:
            spinners = app._kitty_spinners
            time.sleep(0.2)

    threading.Thread(target=current_spinners, daemon=True).start()
    app.run()


def run_one(
    app: Path, setup: str, work: Path, n: int, timeout: float, profile: str, src: Path | None
) -> dict | None:
    mode, spinner = setup.split("/")
    config = work / f"{mode}-{spinner}"
    config.mkdir(exist_ok=True)
    prefs = {
        "smooth_scroll": MODES[mode],
        "smooth_spinner": SPINNERS[spinner],
        "paced_scrollback": "typed",
        "session_host": "off",
    }
    (config / "preferences.json").write_text(json.dumps(prefs))
    mode = f"{mode}-{spinner}"
    out, frames, err = (work / f"{mode}-{n}.{ext}" for ext in ("json", "frames", "err"))
    launcher = work / f"{mode}-{n}.sh"
    q = shlex.quote
    launcher.write_text(
        "#!/bin/sh\n"
        f"cd {q(str(work))}\n"
        + (f"PYTHONPATH={q(str(src))} " if src else "")
        + f"PCODE_CONFIG_DIR={q(str(config))} XDG_STATE_HOME={q(str(work / 'state'))} "
        f"exec {q(sys.executable)} {q(str(Path(__file__).resolve()))} "
        f"--child {q(str(out))} --frames {q(str(frames))} --profile {profile} "
        f"2>{q(str(err))}\n"
    )
    launcher.chmod(0o755)
    command = ["open", "-na", str(app), "--env", f"GHOSTTY_SMOOTH_FRAME_LOG={frames}", "--args"]
    command += ["--window-width=110", "--window-height=40"]
    command += ["--quit-after-last-window-closed=true"]
    subprocess.run([*command, "-e", str(launcher)], check=True)
    # The child `exec`s Python, so only the Ghostty instance has the launcher
    # path on its command line: matching it finds (and kills) that instance.
    deadline = time.monotonic() + timeout
    started = False
    while not out.exists():
        alive = subprocess.run(["pgrep", "-f", str(launcher)], capture_output=True).returncode == 0
        started = started or alive
        if time.monotonic() > deadline or (started and not alive):
            subprocess.run(["pkill", "-f", str(launcher)])
            tail = err.read_text()[-2000:] if err.exists() else ""
            print(f"  {mode} run {n}: no result\n{tail}", file=sys.stderr)
            return None
        time.sleep(0.5)
    time.sleep(1)
    subprocess.run(["pkill", "-f", str(launcher)])
    time.sleep(1)  # let the instance exit before the next run
    result = json.loads(out.read_text())
    if frames.exists():
        with frames.open() as log:
            log.seek(result.pop("frame_log_from"))
            result["frames"] = [json.loads(line) for line in log if line.strip()]
    return result


def frame_summary(runs: list[dict]) -> dict:
    """Frame gaps pooled from every animated stretch of every run."""
    pooled: dict[int, int] = {}
    max_ms = 0.0
    for stretch in (s for run in runs for s in run.get("frames", [])):
        max_ms = max(max_ms, stretch["max_ms"])
        for pair in filter(None, stretch["hist"].split(",")):
            index, count = map(int, pair.split(":"))
            pooled[index] = pooled.get(index, 0) + count
    frames = sum(pooled.values())
    if not frames:
        return {}

    def percentile(q: float) -> float:
        want, seen = max(1, int(frames * q + 0.999999)), 0
        for index in sorted(pooled):
            seen += pooled[index]
            if seen >= want:
                return (index + 1) * BUCKET_MS
        return max_ms

    def at_least(ms: float) -> int:
        return sum(c for i, c in pooled.items() if i >= int(ms / BUCKET_MS))

    return {
        "frames": frames,
        "p50_ms": percentile(0.5),
        "p99_ms": percentile(0.99),
        "max_ms": max_ms,
        "missed_1_plus": at_least(12.5),
        "missed_2_plus": at_least(20.75),
    }


ROWS = [
    ("idle_renders_per_s", "Thinking: renders/s"),
    ("idle_redraws_per_s", "Thinking: redraw requests/s"),
    ("idle_pcode_cpu_pct", "Thinking: pcode CPU %"),
    ("idle_ghostty_cpu_pct", "Thinking: Ghostty CPU %"),
    ("renders", "Streaming: editor renders"),
    ("invalidations", "Streaming: redraw requests"),
    ("handoffs", "Handoffs"),
    ("handoff_ms_total", "Time in handoffs (ms)"),
    ("handoff_ms_p50", "Handoff p50 (ms)"),
    ("pcode_cpu_s", "pcode CPU (s)"),
    ("ghostty_cpu_s", "Ghostty CPU (s)"),
    ("loop_lag_ms_p99", "Loop lag p99 (ms)"),
    ("beats_over_16ms", "Heartbeats >16 ms late"),
]


def report(results: dict[str, list[dict]]) -> None:
    def cell(runs, key):
        values = [r[key] for r in runs if r.get(key) is not None]
        if not values:
            return "-"
        mean = statistics.mean(values)
        spread = f" ±{statistics.stdev(values):.1f}" if len(values) > 1 else ""
        return f"{mean:.1f}{spread}"

    setups = [s for s in results if results[s]]
    heads = " | ".join(f"{s} ({len(results[s])})" for s in setups)
    print(f"\n| Average | {heads} |")
    print("|---" * (len(setups) + 1) + "|")
    for key, label in ROWS:
        print(f"| {label} | " + " | ".join(cell(results[s], key) for s in setups) + " |")
    print("\nRedraw requests by source (mean per run):")
    for s in setups:
        total: dict[str, int] = {}
        for run in results[s]:
            for source, count in run.get("redraw_sources", {}).items():
                total[source] = total.get(source, 0) + count
        top = sorted(total.items(), key=lambda kv: -kv[1])[:6]
        runs = len(results[s])
        print(f"  {s}: " + ", ".join(f"{k} {v / runs:.0f}" for k, v in top))
    if any("idle_redraw_sources" in run for s in setups for run in results[s]):
        print("\nThinking redraw requests by source (mean per run):")
        for s in setups:
            total = {}
            for run in results[s]:
                for source, count in run.get("idle_redraw_sources", {}).items():
                    total[source] = total.get(source, 0) + count
            runs = len(results[s])
            top = sorted(total.items(), key=lambda kv: -kv[1])[:4]
            print(f"  {s}: " + ", ".join(f"{k} {v / runs:.0f}" for k, v in top))
    native = [run for s in setups if s.startswith("native") for run in results[s]]
    gaps = frame_summary(native)
    if gaps:
        print(
            f"\nNative glide frames (pooled): {gaps['frames']}, p50 {gaps['p50_ms']:.2f} ms, "
            f"p99 {gaps['p99_ms']:.2f} ms, max {gaps['max_ms']:.1f} ms. "
            f"At 120 Hz: {gaps['missed_1_plus']} missed at least one vsync (>=12.5 ms), "
            f"{gaps['missed_2_plus']} of them at least two (>=20.8 ms)"
        )
    elif native:
        print(
            "\nNo glide frames logged: the Ghostty build predates GHOSTTY_SMOOTH_FRAME_LOG, "
            "or the windows were hidden (Ghostty stops drawing occluded windows)."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--app", type=Path, default=Path.home() / "p/ghostty-smooth/builds/current")
    parser.add_argument("--timeout", type=float, default=150)
    parser.add_argument("--keep", action="store_true", help="keep the work directory")
    parser.add_argument(
        "--spinners",
        choices=("both", *SPINNERS),
        default="both",
        help="kitty: Kitty-graphics spinners, text: Rich text spinners (default: both)",
    )
    parser.add_argument(
        "--profile",
        choices=PROFILES,
        default="typical",
        help="typical: think 10 s, then ~40 tok/s with pauses; fast: one 180 tok/s burst",
    )
    parser.add_argument(
        "--src",
        type=Path,
        help="run pcode from this checkout's src/ (e.g. a worktree at HEAD, for before/after)",
    )
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--frames", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(args.child, args.frames, PROFILES[args.profile])
        return
    if sys.platform != "darwin":
        sys.exit("pacing_bench drives Ghostty Smooth through `open`; macOS only")
    work = Path(tempfile.mkdtemp(prefix="pacing-bench-"))
    src = None
    if args.src:
        # A checkout root or its src/; an import that falls through to the
        # installed pcode would compare mainline with itself.
        src = args.src.resolve()
        if not (src / "pcode" / "__init__.py").exists():
            src = src / "src"
        if not (src / "pcode" / "__init__.py").exists():
            sys.exit(f"--src: no pcode package in {args.src} or its src/")
    spinners = list(SPINNERS) if args.spinners == "both" else [args.spinners]
    setups = [f"{mode}/{spinner}" for spinner in spinners for mode in MODES]
    results: dict[str, list[dict]] = {setup: [] for setup in setups}
    try:
        for n in range(1, args.runs + 1):
            order = setups if n % 2 else list(reversed(setups))
            for setup in order:
                result = run_one(args.app, setup, work, n, args.timeout, args.profile, src)
                if result is None:
                    continue
                mode, spinner = setup.split("/")
                wrong = []
                if result["gliding"] != (mode == "native"):
                    wrong.append(f"gliding={result['gliding']} (probe {result['probe']})")
                if src and Path(result["pcode_src"]) != src:
                    wrong.append(f"ran pcode from {result['pcode_src']}, not {src}")
                if result["kitty_spinners"] != (spinner == "kitty"):
                    wrong.append(f"kitty_spinners={result['kitty_spinners']}")
                if wrong:
                    report(results)
                    sys.exit(f"{setup} run {n} measured the wrong setup: {', '.join(wrong)}")
                results[setup].append(result)
                mode = setup
                shown = {k: v for k, v in result.items() if k != "frames"}
                print(f"  {mode} run {n}: {json.dumps(shown)}")
        report(results)
    finally:
        if args.keep:
            print(f"\nraw results in {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
