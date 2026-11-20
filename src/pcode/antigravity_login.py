"""`/login antigravity`: run `agy acp --login` so pcode never sees the credential.

Unlike Claude Code and Codex, Antigravity's CLI login prints a Google OAuth URL
whose redirect lands on `antigravity.google/oauth-callback`, and then reads the
resulting authorization code (or full callback URL) from stdin. pcode shows the
URL, opens it in a browser when local, and feeds the pasted code straight to
`agy`'s stdin without echoing or storing it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import webbrowser
from collections.abc import Awaitable, Callable

from pcode.antigravity import COMMAND, child_env
from pcode.auth import LoginError

LOGIN_TIMEOUT_SECONDS = 300.0
RETRY = "/login antigravity"
# OSC (hyperlinks, titles) and CSI (colour, cursor) escapes in CLI output.
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")
REMOTE_VARIABLES = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "NO_BROWSER")
URL_PATTERN = re.compile(r"https://\S+")


def _remote_session() -> bool:
    return any(os.environ.get(name) for name in REMOTE_VARIABLES)


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        # A headless machine is expected; the URL was already displayed.
        pass


def _wants_code(line: str) -> bool:
    lowered = line.lower()
    return "paste" in lowered and ("code" in lowered or "enter" in lowered)


async def antigravity_login(
    notify: Callable[[str], None],
    read_code: Callable[[], Awaitable[str]],
    executable: str | None = None,
    *,
    open_browser: bool = True,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    cancel: str = "Ctrl+C cancels",
) -> None:
    """Run `agy acp --login`, opening the browser URL and forwarding the pasted code."""
    command = executable if executable is not None else shutil.which(COMMAND)
    if not command:
        raise LoginError(
            f"Antigravity CLI (`{COMMAND}`) was not found. Install it, then retry {RETRY}."
        )
    process = await asyncio.create_subprocess_exec(
        command,
        "acp",
        "--login",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=child_env(),
    )
    assert process.stdout is not None and process.stdin is not None
    trailing: list[str] = []
    sent_code = False
    submitted_code = ""
    url_seen = False
    try:
        async with asyncio.timeout(timeout):
            async for raw in process.stdout:
                line = ESCAPES.sub("", raw.decode("utf-8", "replace")).strip()
                if not line:
                    continue
                if not sent_code:
                    match = URL_PATTERN.search(line)
                    if match and not url_seen:
                        url_seen = True
                        url = match.group(0)
                        notify(url)
                        if open_browser and not _remote_session():
                            # Opening a browser can block on some desktops; never stall the loop.
                            await asyncio.to_thread(_open_browser, url)
                        continue
                    if _wants_code(line):
                        notify(
                            "Paste the authorization code (or callback URL) below and "
                            f"press Enter ({cancel})."
                        )
                        submitted_code = (await read_code()).strip()
                        sent_code = True
                        try:
                            process.stdin.write((submitted_code + "\n").encode("utf-8"))
                            await process.stdin.drain()
                        except BrokenPipeError, ConnectionResetError:
                            pass
                        with contextlib.suppress(OSError):
                            process.stdin.close()
                        continue
                trailing.append(line)
            exit_code = await process.wait()
    except TimeoutError:
        raise LoginError(f"Antigravity sign-in timed out. Run {RETRY} to try again.") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if exit_code != 0:
        detail = trailing[-1] if trailing else ""
        if submitted_code and submitted_code in detail:
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise LoginError(
            f"Antigravity sign-in did not complete (exit {exit_code}){suffix}. "
            f"Run {RETRY} to try again."
        )
