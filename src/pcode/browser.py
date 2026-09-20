"""The conversation's browser: one Chromium the bundled `browser` extension drives.

Lives outside the extension because `/reload` re-imports extension modules,
and the browser (with whatever the user logged in to) has to outlive that.
Never persisted: a fresh pcode starts with the browser off, and the cookies of
a session that was logged in leave with the process.

Harness's `PlaywrightBrowser` capability opens a browser per agent run and
closes it when the run ends. pcode runs one agent run per turn, so under it the
user would log in and lose the session at the end of the turn. This module owns
the `PlaywrightBrowserSession` instead and hands the same one to every run,
parent or sub-agent, until `/browser off` or exit.
"""

from __future__ import annotations

import asyncio
from typing import Any

# Chromium is visible: the user logs in by hand in that window, and sees what
# the model does with their session afterwards.
HEADLESS = False
# A coding agent's usual target is its own dev server on localhost, which the
# Harness default (an SSRF guard for untrusted deployments) would refuse.
BLOCK_PRIVATE_ADDRESSES = False
# Every action returns page text; below the Harness default because a browsing
# turn is many actions and each lands in the conversation.
MAX_CONTENT_TOKENS = 2500
LOGIN_TIMEOUT_SECONDS = 300


class BrowserState:
    """Whether the browser tools are offered, and the live session behind them."""

    def __init__(self) -> None:
        self.enabled = False
        self.session: Any = None
        self.toolset: Any = None
        self._armed = False
        self._login_done: asyncio.Event | None = None

    def open(self):
        """Build (not launch) the session and toolset; idempotent."""
        if self.session is None:
            from pydantic_ai_harness.playwright import (
                EgressPolicy,
                PlaywrightBrowserSession,
                PlaywrightBrowserToolset,
            )

            self.session = PlaywrightBrowserSession(
                policy=EgressPolicy(block_private_addresses=BLOCK_PRIVATE_ADDRESSES),
                headless=HEADLESS,
                auto_install_chromium=True,
            )
            self.toolset = PlaywrightBrowserToolset(
                session=self.session, max_content_tokens=MAX_CONTENT_TOKENS
            )
        return self.toolset

    async def arm(self) -> None:
        """Enter the session once, on the loop the tools run on.

        Entering is cheap and launches nothing; Chromium starts on the first
        tool call. Entering twice would discard the driver handle, so it is
        guarded.
        """
        if self.session is not None and not self._armed:
            await self.session.__aenter__()
            self._armed = True

    async def close(self) -> None:
        """Close Chromium if it was started; safe when nothing was."""
        session, self.session, self.toolset = self.session, None, None
        armed, self._armed = self._armed, False
        self._login_done = None
        if session is not None and armed:
            await session.__aexit__(None, None, None)

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


STATE = BrowserState()
