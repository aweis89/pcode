"""Time Pcode.app's session-pane client from spawn to `pcode;ready`, and its idle CPU.

    uv run --no-sync python dev/startup_bench.py              # every scenario
    uv run --no-sync python dev/startup_bench.py --runs 10 --concurrency 10
    uv run --no-sync python dev/startup_bench.py --only attach --idle 0
    uv run --no-sync python dev/startup_bench.py --direnv     # via `direnv exec`, as the app

Each client is spawned the way `PCodeWorkspace.makePane` launches one: in a pty,
`pcode --host` (a new session, so a cold host start) or `pcode --host -c <id>`
(attach to the session's running host), with `PCODE_EDITOR_ONLY=1`,
`PCODE_NATIVE_TRANSCRIPT=1`, `PCODE_HUSH_LOGIN=1` and a fresh `PCODE_HOST_ID`.
It reports the time to the APC `pcode;ready` marker (the app shows the pane) and
to the first `pcode;session;<id>` marker.

Isolated from your real state: `XDG_STATE_HOME`, `XDG_CONFIG_HOME`,
`PCODE_CONFIG_DIR` and `PCODE_HOST_DIR` point into a throwaway directory, no
model is configured (the host runs the offline preview, so nothing calls an
API), and every host it starts is killed at the end.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import statistics
import struct
import subprocess
import sys
import tempfile
import termios
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

READY = b"\x1b_pcode;ready\x1b\\"
# What a terminal answers, so startup probes finish as they do in Ghostty rather
# than waiting out their timeouts: the background color (OSC 11), cursor
# position reports (the ambiguous-width probe sends two around a `─`), and the
# primary device attributes that fence the capability queries.
ANSWERS = (
    (b"\x1b]11;?\x1b\\", b"\x1b]11;rgb:1c1c/1c1c/1c1c\x1b\\"),
    ("\x1b[6n\x1b7─\x1b[6n".encode(), b"\x1b[1;1R\x1b[1;2R"),
    (b"\x1b[6n", b"\x1b[1;1R"),
    (b"\x1b[c", b"\x1b[?62;22c"),
)
QUERIES = re.compile(b"|".join(re.escape(query) for query, _ in ANSWERS))
REPLIES = dict(ANSWERS)
# Pydantic AI's offline TestModel: a real agent and saved sessions, no API calls.
MODEL = ("--model", "test")
SESSION = re.compile(rb"\x1b_pcode;session;([0-9a-f-]*)\x1b\\")


@dataclass
class Client:
    pid: int
    fd: int
    started: float
    host_id: str
    # Attaching, wait for this pane's real session ID; a new session's is empty.
    expect: str = ""
    ready: float | None = None
    session_at: float | None = None
    session_id: str | None = None
    buffer: bytearray = field(default_factory=bytearray)
    closed: bool = False

    def pump(self) -> None:
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            data = b""
        if not data:
            self.closed = True
            return
        now = time.monotonic()
        replies = b"".join(REPLIES[m.group()] for m in QUERIES.finditer(data))
        if replies:
            with contextlib.suppress(OSError):
                os.write(self.fd, replies)
        # Keep a tail so a marker split across reads is still found.
        self.buffer += data
        if self.ready is None and READY in self.buffer:
            self.ready = now - self.started
        if self.session_id is None:
            for match in SESSION.finditer(self.buffer):
                if match.group(1).decode() == self.expect:
                    self.session_at = now - self.started
                    self.session_id = self.expect
                    break
        del self.buffer[:-256]

    def close(self) -> None:
        # Closing the pty's master hangs the client up, as closing the pane does.
        with contextlib.suppress(OSError):
            os.close(self.fd)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            done, _ = os.waitpid(self.pid, os.WNOHANG)
            if done:
                return
            time.sleep(0.02)
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)
        os.waitpid(self.pid, 0)


class Bench:
    def __init__(self, pcode: str, direnv: bool, settings: list[str] = ()) -> None:
        # Short: the host's socket path must fit in sun_path.
        self.root = Path(tempfile.mkdtemp(prefix="pcb", dir="/tmp"))
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.pcode = pcode
        self.direnv = direnv
        self.env = {
            **os.environ,
            "XDG_STATE_HOME": str(self.root / "s"),
            "XDG_CONFIG_HOME": str(self.root / "c"),
            "PCODE_CONFIG_DIR": str(self.root / "c" / "pcode"),
            "PCODE_HOST_DIR": str(self.root / "h"),
            "PCODE_EDITOR_ONLY": "1",
            "PCODE_NATIVE_TRANSCRIPT": "1",
            "PCODE_HUSH_LOGIN": "1",
            "TERM": "xterm-ghostty",
            "TERM_PROGRAM": "ghostty",
            "COLORTERM": "truecolor",
        }
        for name in ("PCODE_SESSION_DIR", "PCODE_PROFILE_DIR", "TMUX", "PCODE_DOCK"):
            self.env.pop(name, None)
        # Hosts would stop 15 s after their last terminal left; keep them for attaching.
        for setting in ["session_host_idle_minutes=off", *settings]:
            key, _, value = setting.partition("=")
            subprocess.run(
                [pcode, "config", "set", key, value],
                env=self.env,
                cwd=self.workspace,
                check=True,
                capture_output=True,
            )

    def spawn(self, *args: str, expect: str = "") -> Client:
        host_id = uuid.uuid4().hex[:8]
        argv = [self.pcode, "--host", *args]
        if self.direnv:
            argv = ["/usr/bin/env", "--", "direnv", "exec", str(self.workspace), *argv]
        env = {**self.env, "PCODE_HOST_ID": host_id}
        started = time.monotonic()
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - the child execs at once.
            try:
                os.chdir(self.workspace)
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(127)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        return Client(pid, fd, started, host_id, expect)

    def run(self, clients: list[Client], timeout: float = 30.0) -> None:
        """Read every client until each has written both markers."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            waiting = [
                c for c in clients if not c.closed and (c.ready is None or c.session_id is None)
            ]
            if not waiting:
                return
            readable, _, _ = select.select([c.fd for c in waiting], [], [], 0.2)
            for client in waiting:
                if client.fd in readable:
                    client.pump()
        missing = [c for c in clients if c.ready is None or c.session_id is None]
        for client in missing:
            print(
                f"{client.host_id}: ready={client.ready} session={client.session_id}",
                file=sys.stderr,
            )
            print(f"  output tail: {bytes(client.buffer)!r}", file=sys.stderr)
        missing = [c.host_id for c in missing]
        raise SystemExit(f"timed out waiting for markers from {missing}; see {self.root}/h/*.log")

    def drain(self, clients: list[Client], seconds: float) -> None:
        """Keep reading so a client never blocks on a full pty."""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            live = [c for c in clients if not c.closed]
            if not live:
                return
            readable, _, _ = select.select([c.fd for c in live], [], [], min(left, 0.5))
            for client in live:
                if client.fd in readable:
                    client.pump()

    def hosts(self) -> dict[str, dict]:
        entries = {}
        for path in (self.root / "h").glob("*.json"):
            with contextlib.suppress(OSError, ValueError):
                entry = json.loads(path.read_text())
                entries[entry["id"]] = entry
        return entries

    def kill_hosts(self) -> None:
        for entry in self.hosts().values():
            with contextlib.suppress(ProcessLookupError, KeyError, TypeError):
                os.kill(entry["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while self.hosts() and time.monotonic() < deadline:
            time.sleep(0.05)
        for entry in self.hosts().values():
            with contextlib.suppress(ProcessLookupError, KeyError, TypeError):
                os.kill(entry["pid"], signal.SIGKILL)
            with contextlib.suppress(OSError):
                (self.root / "h" / f"{entry['id']}.json").unlink()

    def cleanup(self) -> None:
        self.kill_hosts()
        shutil.rmtree(self.root, ignore_errors=True)


def summary(name: str, clients: list[Client]) -> dict:
    ready = sorted(c.ready for c in clients if c.ready is not None)
    session = sorted(c.session_at for c in clients if c.session_at is not None)

    def stats(values: list[float]) -> str:
        if not values:
            return "-"
        p90 = values[min(len(values) - 1, round(0.9 * (len(values) - 1)))]
        median = statistics.median(values)
        return (
            f"median {median * 1000:6.0f} ms  p90 {p90 * 1000:6.0f} ms"
            f"  max {values[-1] * 1000:6.0f} ms"
        )

    print(f"{name:<14} n={len(clients):<3} ready: {stats(ready)}")
    print(f"{'':<14} {'':<5} session: {stats(session)}")
    return {"ready": ready, "session": session}


def cold(bench: Bench, runs: int, concurrency: int) -> list[Client]:
    """New sessions: each client starts its own host."""
    results = []
    for _ in range(runs):
        clients = [bench.spawn(*MODEL) for _ in range(concurrency)]
        bench.run(clients)
        results += clients
        for client in clients:
            client.close()
        bench.kill_hosts()
    return results


def prepare_hosts(bench: Bench, count: int) -> list[str]:
    """Start `count` hosts and leave them running with no terminal; their session IDs.

    A session is saved with its first turn, so each gets one prompt for the
    offline test model to answer.
    """
    clients = [bench.spawn(*MODEL) for _ in range(count)]
    bench.run(clients, timeout=60)
    for client in clients:
        os.write(client.fd, b"hello\r")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        bench.drain(clients, 0.2)
        entries = [e for e in bench.hosts().values() if e["id"] in {c.host_id for c in clients}]
        if len(entries) == count and all(
            e.get("session_id") and e.get("state") == "idle" for e in entries
        ):
            break
    else:
        raise SystemExit(f"hosts did not save their sessions; see {bench.root}/h/*.log")
    for client in clients:
        client.close()
    return [e["session_id"] for e in entries]


def attach(bench: Bench, runs: int, concurrency: int, sessions: list[str]) -> list[Client]:
    """Restored panes: each client attaches to its session's running host."""
    results = []
    for _ in range(runs):
        clients = [bench.spawn("-c", session, expect=session) for session in sessions[:concurrency]]
        bench.run(clients)
        results += clients
        for client in clients:
            client.close()
        time.sleep(0.3)  # Let each host see its terminal go.
    return results


def cpu_seconds(pid: int) -> float | None:
    out = subprocess.run(
        ["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    if not out:
        return None
    seconds = 0.0
    for part in out.replace("-", ":").split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def idle(bench: Bench, seconds: float, sessions: list[str]) -> None:
    """CPU an attached editor-only client and its host use while nothing happens."""
    client = bench.spawn("-c", sessions[0], expect=sessions[0])
    bench.run([client])
    bench.drain([client], 3)  # Past startup work.
    host = next(e for e in bench.hosts().values() if e.get("session_id") == sessions[0])
    lone = next((e for e in bench.hosts().values() if e.get("session_id") != sessions[0]), None)
    pids = {"client": client.pid, "host (attached)": host["pid"]}
    if lone:
        pids["host (detached)"] = lone["pid"]
    before = {name: cpu_seconds(pid) for name, pid in pids.items()}
    bench.drain([client], seconds)
    after = {name: cpu_seconds(pid) for name, pid in pids.items()}
    print(f"idle over {seconds:g} s:")
    for name in pids:
        if before[name] is None or after[name] is None:
            print(f"  {name:<16} exited")
            continue
        used = after[name] - before[name]
        print(f"  {name:<16} {used * 1000:7.0f} ms CPU  ({used / seconds * 100:.2f}%)")
    client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pcode", default=str(Path(sys.executable).parent / "pcode"))
    parser.add_argument("--runs", type=int, default=5, help="rounds per scenario")
    parser.add_argument("--concurrency", type=int, default=10, help="clients per concurrent round")
    parser.add_argument(
        "--idle", type=float, default=30.0, help="seconds of idle CPU sampling; 0 skips"
    )
    parser.add_argument(
        "--direnv", action="store_true", help="launch through `direnv exec` as the app does"
    )
    parser.add_argument("--only", choices=("cold", "attach", "idle"), help="run one scenario")
    parser.add_argument(
        "--set", action="append", default=[], metavar="KEY=VALUE", help="a `pcode config set` first"
    )
    args = parser.parse_args()

    # Absolute: clients run from the throwaway workspace.
    bench = Bench(os.path.abspath(args.pcode), args.direnv, args.set)
    try:
        print(f"pcode: {args.pcode}  state: {bench.root}")
        if args.only in (None, "cold"):
            summary("cold x1", cold(bench, args.runs, 1))
            summary(f"cold x{args.concurrency}", cold(bench, 1, args.concurrency))
        if args.only in (None, "attach", "idle"):
            sessions = prepare_hosts(bench, args.concurrency)
            if args.only in (None, "attach"):
                summary("attach x1", attach(bench, args.runs, 1, sessions))
                summary(
                    f"attach x{args.concurrency}",
                    attach(bench, max(1, args.runs // 2), args.concurrency, sessions),
                )
            if args.idle and args.only in (None, "idle"):
                idle(bench, args.idle, sessions)
    finally:
        bench.cleanup()


if __name__ == "__main__":
    main()
