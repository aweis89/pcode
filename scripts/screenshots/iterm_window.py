"""Play a scene with TABS as real tabs in a new iTerm2 window, and screenshot it.

    uv run --no-sync python scripts/screenshots/iterm_window.py            # tabs
    uv run --no-sync python scripts/screenshots/iterm_window.py --out x.png

Each tab runs `run.py --live <scene> --tab <name>`, so it is the same scripted
session the SVG shot draws, in iTerm2's own tab bar, titles and progress rings.
The window opens at the scene's SIZE; once every tab has played, the scene's
ACTIVE tab is selected and the window is captured with `screencapture`. That
needs Screen Recording permission for the app running this (System Settings >
Privacy & Security); without it you take the shot yourself and press Enter.
"""

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from run import DEMO_ROOT, ITERM_OUT, load, stop_jobs

HERE = Path(__file__).resolve().parent
TIMEOUT = 180


def osascript(script: str) -> str:
    found = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if found.returncode:
        raise SystemExit(f"osascript failed: {found.stderr.strip()}")
    return found.stdout.strip()


def quoted(text: str) -> str:
    """`text` as an AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def open_window(commands: list[str], profile: str, columns: int, rows: int) -> str:
    """A new iTerm2 window with a tab per command, sized to columns x rows; its id."""
    first, *rest = commands
    tabs = "\n".join(
        f"    tell w to create tab with profile {quoted(profile)} command {quoted(c)}" for c in rest
    )
    return osascript(f"""
tell application "iTerm2"
    set w to (create window with profile {quoted(profile)} command {quoted(first)})
    tell current session of w
        set columns to {columns}
        set rows to {rows}
    end tell
{tabs}
    tell first tab of w to select
    return id of w
end tell""")


def tab_titles(window: str) -> list[str]:
    names = osascript(f"""
tell application "iTerm2"
    set out to {{}}
    repeat with t in tabs of (window id {window})
        set end of out to name of current session of t
    end repeat
    set AppleScript's text item delimiters to linefeed
    return out as text
end tell""")
    return names.splitlines()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scene", nargs="?", default="tabs", help="a scene with TABS")
    parser.add_argument("--out", type=Path, help=f"PNG to write ({ITERM_OUT}/<scene>-iterm.png)")
    parser.add_argument("--profile", default="Default", help="iTerm2 profile (Default)")
    parser.add_argument("--keep", action="store_true", help="leave the window open afterwards")
    args = parser.parse_args()
    scene = load(args.scene)
    tabs = list(getattr(scene, "TABS", {}))
    if not tabs:
        parser.error(f"{args.scene} has no TABS")
    out = args.out or ITERM_OUT / f"{args.scene}-iterm.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    columns, rows = getattr(scene, "SIZE", (100, 30))
    with tempfile.TemporaryDirectory(prefix="pcode-tabs-") as tmp:
        ready = Path(tmp)
        go = ready / "go"
        commands = []
        for tab in tabs:
            # Each tab waits for `go`, so it starts at the size set after the
            # window opens rather than the profile's; errors land in a log.
            run = shlex.join(
                [sys.executable, str(HERE / "run.py"), "--live", args.scene]
                + ["--tab", tab, "--ready", str(ready / tab)]
            )
            log = shlex.quote(str(ready / f"{tab}.log"))
            script = ready / f"{tab}.sh"
            # iTerm2 runs a tab's command with a bare PATH: no Homebrew tmux.
            script.write_text(
                f"export PATH={shlex.quote(os.environ['PATH'])}\n"
                f"while [ ! -e {shlex.quote(str(go))} ]; do sleep 0.1; done\n"
                f"{run} 2>{log} || {{ cat {log}; sleep 60; }}\n"
            )
            commands.append(f"/bin/sh {script}")
        window = open_window(commands, args.profile, columns, rows)
        servers = []
        try:
            time.sleep(1)
            go.touch()
            deadline = time.monotonic() + TIMEOUT
            while not all((ready / tab).exists() for tab in tabs):
                if time.monotonic() > deadline:
                    missing = [tab for tab in tabs if not (ready / tab).exists()]
                    logs = "".join(
                        f"--- {tab}\n{(ready / f'{tab}.log').read_text()}"
                        for tab in missing
                        if (ready / f"{tab}.log").exists()
                    )
                    raise SystemExit(f"tabs never finished: {', '.join(missing)}\n{logs}")
                time.sleep(0.5)
            servers = [(ready / tab).read_text() for tab in tabs]
            active = tabs.index(scene.ACTIVE) + 1
            osascript(
                f'tell application "iTerm2" to tell tab {active} of window id {window} to select'
            )
            osascript('tell application "iTerm2" to activate')
            # Titles and progress are sampled once a second; let the tab bar catch up.
            time.sleep(2.5)
            for tab, title in zip(tabs, tab_titles(window), strict=False):
                print(f"{tab}: {title}")
            shot = subprocess.run(
                ["screencapture", "-x", "-o", f"-l{window}", str(out)], capture_output=True
            )
            if shot.returncode or not out.exists():
                print("screencapture failed: no Screen Recording permission for this terminal?")
                if not sys.stdin.isatty():
                    raise SystemExit(1)
                print("Take the shot yourself (Cmd-Shift-4, Space, click the window), then Enter.")
                input()
            else:
                print(out)
            if args.keep and sys.stdin.isatty():
                print("Press Enter to close the window.")
                input()
        finally:
            # Each tab's run.py cleans up once its tmux server goes, and exits.
            for server in servers:
                subprocess.run(["tmux", "-L", server, "kill-server"], capture_output=True)
            time.sleep(1.5)
            # A tab that never got that far: its tmux server runs under the demo root.
            stop_jobs(DEMO_ROOT.resolve())
            close = f'tell application "iTerm2" to close window id {window}'
            subprocess.run(["osascript", "-e", close], capture_output=True)


if __name__ == "__main__":
    main()
