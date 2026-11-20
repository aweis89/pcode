"""`/login antigravity`: run `agy acp --login` so pcode never sees the credential.

Unlike Claude Code and Codex, Antigravity's CLI login prints a Google OAuth URL
whose redirect lands on `antigravity.google/oauth-callback`, and then reads the
resulting authorization code (or full callback URL) from stdin. pcode shows the
URL, opens it in a browser when local, and feeds the pasted code straight to
`agy`'s stdin without echoing or storing it.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import re
import shutil
import subprocess
import webbrowser
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qsl, unquote, urlsplit

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


def _secret_tokens(submitted: str) -> list[str]:
    """Substrings of a pasted code or callback URL that must never appear in errors."""
    if not submitted:
        return []
    tokens: list[str] = [submitted]
    decoded = unquote(submitted)
    if decoded != submitted:
        tokens.append(decoded)
    parsed = urlsplit(submitted)
    if parsed.query:
        for _key, value in parse_qsl(parsed.query, keep_blank_values=False):
            if len(value) >= 4:
                tokens.append(value)
                unquoted = unquote(value)
                if unquoted != value:
                    tokens.append(unquoted)
        # Also catch raw percent-encoded values in the query string.
        for pair in parsed.query.split("&"):
            _, sep, raw_val = pair.partition("=")
            if sep and len(raw_val) >= 4:
                tokens.append(raw_val)
    return tokens


def _contains_secret(text: str, submitted: str) -> bool:
    return any(token in text for token in _secret_tokens(submitted))


async def antigravity_login(
    notify: Callable[[str], None],
    read_code: Callable[[], Awaitable[str | None]],
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
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""

    async def handle_line(line: str, *, partial: bool = False) -> bool:
        """Inspect one line of CLI output; returns True if the code prompt was handled."""
        nonlocal sent_code, submitted_code, url_seen
        if not line:
            return False
        if not sent_code:
            match = URL_PATTERN.search(line)
            if match and not url_seen:
                url_seen = True
                url = match.group(0)
                notify(url)
                if open_browser and not _remote_session():
                    # Opening a browser can block on some desktops; never stall the loop.
                    await asyncio.to_thread(_open_browser, url)
                if not _wants_code(line):
                    return False
            if _wants_code(line):
                notify(f"Paste the authorization code (or callback URL) in the prompt ({cancel}).")
                code = await read_code()
                if code is None:
                    raise asyncio.CancelledError
                submitted_code = code.strip()
                sent_code = True
                notify("Completing Antigravity sign-in\u2026")
                try:
                    process.stdin.write((submitted_code + "\n").encode("utf-8"))
                    await process.stdin.drain()
                except BrokenPipeError, ConnectionResetError:
                    pass
                with contextlib.suppress(OSError):
                    process.stdin.close()
                return True
        if not partial:
            trailing.append(line)
        return False

    try:
        async with asyncio.timeout(timeout):
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    rest = pending + decoder.decode(b"", final=True)
                    cleaned = ESCAPES.sub("", rest.replace("\r", "")).strip()
                    if cleaned:
                        await handle_line(cleaned)
                    break
                pending += decoder.decode(chunk)
                while "\n" in pending:
                    raw_line, pending = pending.split("\n", 1)
                    cleaned = ESCAPES.sub("", raw_line.replace("\r", "")).strip()
                    await handle_line(cleaned)
                if not sent_code and pending:
                    cleaned_partial = ESCAPES.sub("", pending.replace("\r", "")).strip()
                    if _wants_code(cleaned_partial):
                        if await handle_line(cleaned_partial, partial=True):
                            pending = ""
            exit_code = await process.wait()
    except TimeoutError:
        raise LoginError(f"Antigravity sign-in timed out. Run {RETRY} to try again.") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if exit_code != 0:
        detail = trailing[-1] if trailing else ""
        if detail and _contains_secret(detail, submitted_code):
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise LoginError(
            f"Antigravity sign-in did not complete (exit {exit_code}){suffix}. "
            f"Run {RETRY} to try again."
        )
