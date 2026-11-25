#!/usr/bin/env python3
"""Inspect and drive a Ghostty/Pcode.app build through its debug socket.

The app listens when started with GHOSTTY_DEBUG_SOCKET set (see `launch`).
Points are window content points from the top-left; `--cell COL ROW` with
`-s SURFACE` targets the middle of a terminal cell instead. Surface IDs are
the 8-character prefixes `dump` prints. Stdlib only: run it with `python3 -I`.

    gdbg.py launch --app ~/p/ghostty-x/builds/current --command /tmp/run.sh
    gdbg.py dump                   # windows, terminals, frames, focus
    gdbg.py snap                   # PNG of the front window; open it with read_file
    gdbg.py key ctrl+a '\\'         # keystrokes to the focused terminal
    gdbg.py drag --from-cell 0 2 --to-cell 20 2 -s 1a2b3c4d   # then reports the selection
    gdbg.py record --seconds 2     # frames + flagged one-frame flashes
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_SOCKET = Path.home() / ".local/state/pcode/ghostty-debug.sock"


def socket_path(args: argparse.Namespace) -> str:
    return args.socket or os.environ.get("GHOSTTY_DEBUG_SOCKET") or str(DEFAULT_SOCKET)


def call(path: str, request: dict, timeout: float = 60) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(json.dumps(request).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(1 << 20)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def target(args: argparse.Namespace, request: dict, key: str = "at") -> None:
    """Add a point (`x y`) or cell (`--cell`) target to `request`."""
    cell = getattr(args, f"{key}_cell" if key != "at" else "cell", None)
    if cell:
        request["cell" if key == "at" else f"{key}_cell"] = cell
        return
    point = getattr(args, key if key != "at" else "point", None)
    if point:
        request[key] = point


def common(args: argparse.Namespace, request: dict) -> dict:
    if getattr(args, "surface", None):
        request["surface"] = args.surface
    if getattr(args, "window", None) is not None:
        request["window"] = args.window
    return request


def print_dump(reply: dict) -> None:
    for window in reply["windows"]:
        flags = " ".join(f for f in ("key", "main") if window.get(f))
        if not window.get("on_screen", True):
            flags += " covered(not drawing)"
        size = "x".join(str(int(v)) for v in window["content_size"])
        print(
            f"window {window['index']} #{window['number']} {window['class']} {size}pt "
            f"@{window['scale']}x {flags} responder={window.get('first_responder', '-')}"
        )
        print(f"  title: {window['title']!r}")
        for s in window["surfaces"]:
            grid = "x".join(map(str, s.get("grid", []))) or "?"
            marks = [
                m for m in ("first_responder", "focused", "hidden", "has_selection") if s.get(m)
            ]
            role = (
                f"overlay={s['overlay']} of {s.get('overlay_of', '?')}"
                if "overlay" in s
                else "pane"
            )
            print(
                f"  surface {s['id']} {role} grid={grid} frame={s.get('frame')} "
                f"visible={s.get('visible')} {' '.join(marks)}"
            )
            if s.get("title"):
                print(f"    title: {s['title']!r}")


def print_tree(node: dict, depth: int = 0, max_depth: int = 99) -> None:
    keys = [k for k in node if k not in ("class", "frame", "children")]
    extra = " ".join(f"{k}={node[k]}" for k in keys)
    print(f"{'  ' * depth}{node['class']} {node.get('frame')} {extra}".rstrip())
    if depth < max_depth:
        for child in node.get("children", []):
            print_tree(child, depth + 1, max_depth)


def launch(args: argparse.Namespace) -> int:
    """Start a build with the socket on, replacing any instance already on it."""
    path = socket_path(args)
    try:
        old = call(path, {"cmd": "ping"}, timeout=2)
        os.kill(old["pid"], 15)
        for _ in range(50):
            try:
                os.kill(old["pid"], 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
    except OSError, ValueError, KeyError:
        pass
    app = Path(args.app).expanduser()
    binary = app / "Contents/MacOS/ghostty" if app.suffix == ".app" or app.is_dir() else app
    env = dict(os.environ, GHOSTTY_DEBUG_SOCKET=path)
    command = [str(binary)]
    if args.command:
        command.append(f"--command={args.command}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    log = open(args.log, "a")  # noqa: SIM115 - handed to the child
    proc = subprocess.Popen(command, env=env, stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            reply = call(path, {"cmd": "dump"}, timeout=2)
            if reply.get("ok") and any(w["surfaces"] for w in reply["windows"]):
                print(f"pid {proc.pid} socket {path}")
                print_dump(reply)
                return 0
        except OSError, ValueError:
            pass
        if proc.poll() is not None:
            print(f"app exited with {proc.returncode}; see {args.log}", file=sys.stderr)
            return 1
        time.sleep(0.25)
    print(f"no terminal on {path} after {args.timeout}s; see {args.log}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--socket", help=f"socket path (default $GHOSTTY_DEBUG_SOCKET or {DEFAULT_SOCKET})"
    )
    parser.add_argument("--json", action="store_true", help="print the raw JSON reply")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(
        name: str, help: str, *, surface: bool = True, window: bool = True, point: str | None = None
    ):
        p = sub.add_parser(name, help=help)
        if surface:
            p.add_argument("-s", "--surface", help="surface ID prefix (default: focused)")
        if window:
            p.add_argument("-w", "--window", type=int, help="window index from dump")
        if point:
            dest = "point" if point == "at" else point
            p.add_argument(
                dest, nargs="*", type=float, metavar="XY", help=f"{point} point in window points"
            )
            p.add_argument(
                f"--{point}-cell" if point != "at" else "--cell",
                type=int,
                nargs=2,
                metavar=("COL", "ROW"),
                dest=f"{point}_cell" if point != "at" else "cell",
            )
        return p

    p = sub.add_parser("launch", help="start an app build with the socket on")
    p.add_argument("--app", required=True, help=".app bundle (or its binary)")
    p.add_argument("--command", help="Ghostty --command for the first terminal")
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--log", default="/tmp/gdbg-app.log")

    add("ping", "is the app listening", surface=False, window=False)
    add(
        "dump", "windows and terminals: frames, grids, focus, overlays", surface=False, window=False
    )
    p = add("text", "a terminal's text", window=False)
    p.add_argument("--scope", choices=["viewport", "screen", "selection"], default="viewport")
    p = add("wait", "wait until a terminal's text matches a regex", window=False)
    p.add_argument("pattern")
    p.add_argument("--timeout", type=float, default=10)
    p.add_argument("--absent", action="store_true", help="wait until it no longer matches")
    p.add_argument("--scope", choices=["viewport", "screen"], default="viewport")
    for name in ("views", "layers"):
        p = add(name, f"the window's {name[:-1]} tree", surface=False)
        p.add_argument("--depth", type=int, default=40)
    add("hit", "which view a click at a point reaches", point="at")
    add(
        "activate",
        "bring the app and window to the front (takes focus from the user)",
        surface=False,
    )
    add("focus", "make a terminal the window's first responder", window=False)
    p = add("key", "keystrokes like ctrl+a '\\' enter cmd+shift+]")
    p.add_argument("keys", nargs="+")
    p.add_argument("--gap", type=float, default=0.05)
    p = add("type", "type text, one key at a time")
    p.add_argument("text")
    p = add("click", "click at a point", point="at")
    p.add_argument("--right", action="store_true")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--mods", help="e.g. cmd+shift")
    p = add("drag", "drag between two points; reports the selection")
    p.add_argument("coords", nargs="*", type=float, metavar="X1 Y1 X2 Y2")
    p.add_argument("--from-cell", type=int, nargs=2, metavar=("COL", "ROW"))
    p.add_argument("--to-cell", type=int, nargs=2, metavar=("COL", "ROW"))
    p.add_argument("--steps", type=int, default=12)
    p.add_argument("--mods")
    add("move", "move the pointer to a point", point="at")
    p = add("scroll", "scroll lines at a point (positive: back)", point="at")
    p.add_argument("--dy", type=int, default=3)
    for name in ("snap", "record"):
        p = add(name, "PNG of a window" if name == "snap" else "frames and flagged flashes")
        p.add_argument("--crop", action="store_true", help="crop to the surface's visible part")
        p.add_argument("--rect", type=float, nargs=4, metavar=("X", "Y", "W", "H"))
        p.add_argument("--scale", type=float, help="pixels per point")
        p.add_argument("--retina", action="store_true", help="full backing resolution")
    sub.choices["snap"].add_argument(
        "--raw", action="store_true", help="the terminal's own last frame, unclipped (with -s)"
    )
    sub.choices["snap"].add_argument("path", nargs="?")
    rec = sub.choices["record"]
    rec.add_argument("--seconds", type=float, default=2)
    rec.add_argument("--fps", type=float, default=30)
    rec.add_argument("--dir")
    p = sub.add_parser("raw", help="send a JSON request as is")
    p.add_argument("request")

    args = parser.parse_args()
    if args.cmd == "launch":
        return launch(args)

    request: dict = {"cmd": args.cmd}
    if args.cmd == "raw":
        request = json.loads(args.request)
    common(args, request)
    if args.cmd in ("hit", "click", "move", "scroll"):
        target(args, request)
        if request.get("at") is not None and len(request["at"]) != 2:
            parser.error("a point is X Y")
        if not request.get("at"):
            request.pop("at", None)
    if args.cmd == "drag":
        if args.coords:
            if len(args.coords) != 4:
                parser.error("drag takes X1 Y1 X2 Y2")
            request["from"], request["to"] = args.coords[:2], args.coords[2:]
        if args.from_cell:
            request["from_cell"] = args.from_cell
        if args.to_cell:
            request["to_cell"] = args.to_cell
        request["steps"] = args.steps
    for name in (
        "scope",
        "pattern",
        "timeout",
        "depth",
        "keys",
        "gap",
        "text",
        "count",
        "mods",
        "dy",
        "path",
        "rect",
        "scale",
        "seconds",
        "fps",
        "dir",
    ):
        value = getattr(args, name, None)
        if value is not None:
            request[name] = value
    for flag in ("absent", "crop", "retina", "raw"):
        if getattr(args, flag, False):
            request[flag] = True
    if getattr(args, "right", False):
        request["button"] = "right"

    timeout = 60 + float(request.get("seconds", 0)) + float(request.get("timeout", 0))
    try:
        reply = call(socket_path(args), request, timeout=timeout)
    except FileNotFoundError, ConnectionRefusedError:
        print(
            f"nothing listening on {socket_path(args)}; start the app with `launch`",
            file=sys.stderr,
        )
        return 2
    ok = bool(reply.get("ok"))
    if args.json or args.cmd == "raw":
        print(json.dumps(reply, indent=2))
    elif not reply.get("ok"):
        print(f"error: {reply.get('error')}", file=sys.stderr)
        if "text" in reply:
            print(reply["text"])
    elif args.cmd == "dump":
        print_dump(reply)
    elif args.cmd in ("text", "wait"):
        print(reply["text"])
    elif args.cmd in ("views", "layers"):
        print_tree(reply["tree"])
    elif args.cmd == "snap":
        print(reply["path"], "x".join(map(str, reply["size"])))
    elif args.cmd == "record":
        print(f"{reply['frames']} frames in {reply['dir']} (index.json has per-frame diffs)")
        for flash in reply["flashes"]:
            frame, t, score = flash["frame"], flash["t"], flash["score"]
            print(f"  flash: frame {frame:04d} at {t:.3f}s score {score:.3f}")
        print(f"  biggest changes: {reply['biggest_changes']}")
    else:
        reply.pop("ok", None)
        print(json.dumps(reply, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
