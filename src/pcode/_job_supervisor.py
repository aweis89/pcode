"""Detached supervisor: owns one command's lifetime and publishes its exit status.

Runs in its own interpreter and its own session so the command survives agent
run teardown, event-loop shutdown, and pcode itself. It writes `status.json`
atomically, which is the only thing the parent polls: a supervisor killed
between publications leaves the last consistent snapshot rather than a partial
file.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:  # pragma: no cover -- exercised through a real subprocess.
    directory = Path(sys.argv[1])
    command = sys.argv[2]
    status = directory / "status.json"
    pending = directory / "status.tmp"

    def publish(*, pid: int, exit_code: int | None, started_at: float) -> None:
        pending.write_text(
            json.dumps(
                {
                    "pid": pid,
                    "exit_code": exit_code,
                    "started_at": started_at,
                    "ended_at": None if exit_code is None else time.time(),
                }
            ),
            encoding="utf-8",
        )
        pending.replace(status)

    with (directory / "output.log").open("ab", buffering=0) as output:
        started_at = time.time()
        process = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
        )
        publish(pid=process.pid, exit_code=None, started_at=started_at)
        exit_code = process.wait()
        publish(pid=process.pid, exit_code=exit_code, started_at=started_at)


if __name__ == "__main__":  # pragma: no cover
    # The parent starts a new session for us. Ignore terminal hangups without
    # changing how the child command itself handles termination.
    if os.name != "nt":
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    main()
