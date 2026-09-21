"""`/login openai-codex` and `/logout openai-codex`, delegated to the Codex CLI.

Pydantic AI reads the credential file the `codex` CLI writes (`$CODEX_HOME/auth.json`)
and never writes it, so the CLI stays the owner of that store: pcode runs
`codex login` and relays what it prints (the sign-in URL when no browser opens)
into the transcript. Pydantic AI's own `OpenAICodexOAuthFlow` was considered and
rejected: it drops the `id_token` the CLI requires in `auth.json`, so pcode
would have written a file the CLI itself could not read.

The subprocess gets pipes, never the terminal: prompt_toolkit owns the tty and
`codex login` only prints and waits for its localhost callback.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable

from pcode.auth import LoginError

LOGIN_TIMEOUT_SECONDS = 300.0
INSTALL_HINT = (
    "Install the Codex CLI (https://github.com/openai/codex) and make sure `codex` is on PATH."
)


def codex_executable() -> str:
    path = shutil.which("codex")
    if not path:
        raise LoginError(f"The `codex` command was not found. {INSTALL_HINT}")
    return path


async def _run(*arguments: str, notify: Callable[[str], object] | None, timeout: float) -> None:
    """Run a `codex` subcommand, relaying non-empty output lines to `notify`."""
    executable = codex_executable()
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as error:
        raise LoginError(f"Could not start `codex {' '.join(arguments)}`: {error}") from None
    assert process.stdout is not None

    async def relay() -> None:
        async for raw in process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line and notify is not None:
                notify(line)

    try:
        async with asyncio.timeout(timeout):
            await relay()
            await process.wait()
    except TimeoutError:
        raise LoginError(
            f"`codex {' '.join(arguments)}` timed out after {int(timeout)}s. "
            "Run /login openai-codex to try again."
        ) from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        raise LoginError(f"`codex {' '.join(arguments)}` exited with status {process.returncode}.")


async def login(
    *, notify: Callable[[str], object] | None = None, timeout: float = LOGIN_TIMEOUT_SECONDS
) -> None:
    """Sign in with the Codex CLI; the CLI opens the browser and writes `auth.json`."""
    await _run("login", notify=notify, timeout=timeout)


async def logout(*, timeout: float = 30.0) -> None:
    """Remove the Codex CLI's stored credentials."""
    await _run("logout", notify=None, timeout=timeout)
