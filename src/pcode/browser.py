"""The conversation's browser: one real Chrome the bundled `browser` extension drives.

Lives outside the extension because `/reload` re-imports extension modules,
and the browser (with whatever the user logged in to) has to outlive that.
Never persisted: a fresh pcode starts with the browser off, and the cookies of
a session that was logged in leave with the process.

Two decisions differ from Harness's own `PlaywrightBrowser` capability:

- The session is owned here, not by the capability. That capability opens a
  browser per agent run and closes it when the run ends; pcode runs one agent
  run per turn, so under it the user would log in and lose the session at the
  end of the turn. One `PlaywrightBrowserSession` is handed to every run,
  parent or sub-agent, until `/browser off` or exit.
- The browser is the user's installed Chrome, attached over CDP, not
  Playwright's Chromium. Playwright launches its build with `--enable-automation`
  (`navigator.webdriver` is true) and no Google API keys, and Google refuses to
  sign in to that browser ("controlled through software automation"). Chrome
  started by us with only a debugging port and its own profile carries neither
  mark. `PCODE_BROWSER_CDP_URL` attaches to a Chrome started some other way
  instead, and when no Chrome is installed the session falls back to
  Playwright's Chromium, which works everywhere except such sign-in pages.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

# A coding agent's usual target is its own dev server on localhost, which the
# Harness default (an SSRF guard for untrusted deployments) would refuse.
BLOCK_PRIVATE_ADDRESSES = False
# Every action returns page text; below the Harness default because a browsing
# turn is many actions and each lands in the conversation.
MAX_CONTENT_TOKENS = 2500
LOGIN_TIMEOUT_SECONDS = 300
CHROME_START_SECONDS = 20
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def chrome_executable() -> str | None:
    """The Chrome to launch: `PCODE_BROWSER_CHROME`, else the first installed candidate."""
    configured = os.environ.get("PCODE_BROWSER_CHROME", "").strip()
    if configured:
        return configured
    for candidate in CHROME_CANDIDATES:
        if "/" in candidate:
            if Path(candidate).is_file():
                return candidate
        elif shutil.which(candidate):
            return candidate
    return None


def profile_dir() -> Path:
    """Chrome's profile for the agent, apart from the user's own so no login leaks in."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "chrome"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class BrowserState:
    """Whether the browser tools are offered, and the live session behind them."""

    def __init__(self) -> None:
        self.enabled = False
        self.session: Any = None
        self.toolset: Any = None
        self.cdp_url: str | None = None
        self.process: subprocess.Popen | None = None
        self._armed = False
        self._launch_lock: asyncio.Lock | None = None
        self._login_done: asyncio.Event | None = None

    @property
    def attached(self) -> bool:
        """Attaching to a Chrome someone else started, which is theirs to close."""
        return bool(os.environ.get("PCODE_BROWSER_CDP_URL", "").strip())

    def open(self):
        """Build (not launch) the session and toolset; idempotent."""
        if self.session is None:
            from pydantic_ai_harness.playwright import (
                EgressPolicy,
                PlaywrightBrowserSession,
                PlaywrightBrowserToolset,
            )

            if self.attached:
                self.cdp_url = os.environ["PCODE_BROWSER_CDP_URL"].strip()
            elif chrome_executable():
                # The port is chosen now so the session can be built before the
                # browser exists; Chrome starts on the first tool call.
                self.cdp_url = f"http://127.0.0.1:{_free_port()}"
            else:
                self.cdp_url = None
            self.session = PlaywrightBrowserSession(
                policy=EgressPolicy(block_private_addresses=BLOCK_PRIVATE_ADDRESSES),
                headless=False,
                cdp_url=self.cdp_url,
                auto_install_chromium=True,
            )
            self.toolset = PlaywrightBrowserToolset(
                session=self.session, max_content_tokens=MAX_CONTENT_TOKENS
            )
        return self.toolset

    async def arm(self) -> None:
        """Enter the session once, on the loop the tools run on.

        Entering is cheap and launches nothing. Entering twice would discard the
        driver handle, so it is guarded.
        """
        if self.session is not None and not self._armed:
            await self.session.__aenter__()
            self._armed = True

    async def ensure_chrome(self) -> str:
        """Start Chrome on the debugging port unless one is already answering there.

        Returns a line describing what happened, for a notice. Nothing to do
        when attaching to a foreign endpoint or falling back to Chromium.
        """
        if self.cdp_url is None or self.attached:
            return ""
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()
        async with self._launch_lock:
            if self.process is not None and self.process.poll() is None:
                return ""
            if await self._answering():
                return ""
            executable = chrome_executable()
            if executable is None:  # pragma: no cover - open() would have chosen Chromium
                return ""
            port = self.cdp_url.rsplit(":", 1)[1]
            profile = profile_dir()
            profile.mkdir(parents=True, exist_ok=True)
            self.process = subprocess.Popen(
                [
                    executable,
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "about:blank",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = asyncio.get_running_loop().time() + CHROME_START_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"Chrome exited at startup (code {self.process.returncode})."
                    )
                if await self._answering():
                    return f"Started Chrome ({executable}) with profile {profile}."
                await asyncio.sleep(0.2)
            raise RuntimeError(
                f"Chrome did not open its debugging port within {CHROME_START_SECONDS}s."
            )

    async def _answering(self) -> bool:
        assert self.cdp_url is not None
        import httpx

        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                response = await client.get(self.cdp_url.rstrip("/") + "/json/version")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        """Disconnect, then quit the Chrome we started; safe when nothing was."""
        session, self.session, self.toolset = self.session, None, None
        armed, self._armed = self._armed, False
        process, self.process = self.process, None
        self._login_done = None
        self.cdp_url = None
        try:
            if session is not None and armed:
                await session.__aexit__(None, None, None)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 5)
                except subprocess.TimeoutExpired:
                    process.kill()

    @property
    def launched(self) -> bool:
        return self.session is not None and self.session.page is not None

    def login_event(self) -> asyncio.Event:
        if self._login_done is None:
            self._login_done = asyncio.Event()
        return self._login_done

    async def wait_for_login(self, done_url_prefix: str | None) -> str:
        """Block until the user reports done, the page reaches the prefix, or the deadline.

        Returns which of the three happened.
        """
        event = self.login_event()
        event.clear()
        page = self.session.page if self.session is not None else None
        deadline = asyncio.get_running_loop().time() + LOGIN_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if event.is_set():
                return "user"
            if done_url_prefix and page is not None and page.url.startswith(done_url_prefix):
                return "url"
            await asyncio.sleep(0.5)
        return "timeout"

    def describe(self) -> str:
        """One line for `/browser status`."""
        if not self.enabled:
            return "off"
        if self.cdp_url is None:
            how = "Playwright's Chromium (no Chrome found; Google sign-in will refuse it)"
        elif self.attached:
            how = f"attached to {self.cdp_url}"
        else:
            how = f"Chrome on {self.cdp_url}" + (
                "" if self.process is not None and self.process.poll() is None else " (not started)"
            )
        page = self.session.page if self.launched else None
        return f"on, {how}" + (f", at {page.url}" if page is not None else "")


STATE = BrowserState()
