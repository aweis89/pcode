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
  mark. When no Chrome is installed the session falls back to Playwright's
  Chromium, which works everywhere except such sign-in pages.
- The page opens in the browser's own default context rather than a fresh one
  (`SharedContextSession`). Harness isolates every run in a new context so a
  run never inherits a login; here inheriting is the point. On the Chrome we
  launch, the default context is the pcode profile on disk, so a login made
  once holds across pcode sessions. `/browser attach` (or
  `PCODE_BROWSER_CDP_URL`) joins the user's everyday Chrome instead, where the
  default context holds every account they are signed in to; that is the
  higher-risk choice and is never the default.
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


def _chrome_profile_dirs() -> list[Path]:
    """Where the everyday Chrome keeps `DevToolsActivePort` once remote debugging is on."""
    home = Path.home()
    return [
        home / "Library" / "Application Support" / "Google" / "Chrome",
        home / "Library" / "Application Support" / "Chromium",
        home / ".config" / "google-chrome",
        home / ".config" / "chromium",
    ]


def running_chrome_url() -> str | None:
    """The CDP endpoint of the user's own Chrome, if it has remote debugging on.

    `PCODE_BROWSER_CDP_URL` wins. Otherwise Chrome writes `DevToolsActivePort`
    (port, then the browser websocket path) into its profile when started with
    `--remote-debugging-port` or when `chrome://inspect/#remote-debugging` is
    toggled on; `PCODE_BROWSER_PORT_FILE` names that file elsewhere.
    """
    configured = os.environ.get("PCODE_BROWSER_CDP_URL", "").strip()
    if configured:
        return configured
    named = os.environ.get("PCODE_BROWSER_PORT_FILE", "").strip()
    files = [Path(named)] if named else [d / "DevToolsActivePort" for d in _chrome_profile_dirs()]
    for file in files:
        try:
            port, path = file.read_text().split()[:2]
        except (OSError, ValueError):
            continue
        return f"ws://127.0.0.1:{int(port)}{path}"
    return None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _session_class():
    from pydantic_ai_harness.playwright import PlaywrightBrowserSession

    class SharedContextSession(PlaywrightBrowserSession):
        """A session whose page lives in the browser's default context.

        Overrides the private `_launch` of the pinned Harness revision: the
        only difference is `browser.contexts[0]` in place of `new_context()`
        when the browser has one, so the page shares that context's cookies.
        The guards Harness installs (route, websocket, page wiring) are kept.
        A browser reached over CDP is left running by Harness's own teardown,
        which only disconnects; the page opened here is closed first so no
        stray tab is left in the user's window.
        """

        async def _launch(self) -> None:
            assert self._driver_cm is not None
            if self._driver is None:
                self._driver = await self._driver_cm.__aenter__()
                self._driver_entered = True
            if self._browser is not None:
                stale = self._browser
                await self._bounded(stale.close())
                self._browser = None
            browser = await self._connect(self._driver)
            if browser is None:
                return
            self._browser = browser
            if browser.contexts:
                context = browser.contexts[0]
            else:  # pragma: no cover - a launched Chromium has none until asked
                context = await self._bounded(
                    browser.new_context(service_workers="block", accept_downloads=False)
                )
            self._context = context
            page = await self._bounded(context.new_page())
            if self.policy.enforced():
                await self._bounded(context.route("**/*", self._route_guard))
                await self._bounded(context.route_web_socket("**/*", self._websocket_guard))
            self._wire_page(page)
            self.pages.append(page)
            self.page = page

        async def __aexit__(self, exc_type, *rest) -> None:
            for page in list(self.pages):
                try:
                    await page.close()
                except Exception:  # noqa: BLE001 - the tab may already be gone.
                    pass
            await super().__aexit__(exc_type, *rest)

    return SharedContextSession


class BrowserState:
    """Whether the browser tools are offered, and the live session behind them."""

    def __init__(self) -> None:
        self.enabled = False
        self.attach = False
        self.session: Any = None
        self.toolset: Any = None
        self.cdp_url: str | None = None
        self.process: subprocess.Popen | None = None
        self._armed = False
        self._launch_lock: asyncio.Lock | None = None

    @property
    def attached(self) -> bool:
        """Joined a Chrome someone else started, which is theirs to close."""
        return self.attach

    def open(self):
        """Build (not launch) the session and toolset; idempotent.

        Raises `ValueError` in attach mode when no running Chrome is found.
        """
        if self.session is None:
            from pydantic_ai_harness.playwright import EgressPolicy, PlaywrightBrowserToolset

            if self.attach:
                self.cdp_url = running_chrome_url()
                if self.cdp_url is None:
                    raise ValueError(
                        "No running Chrome with remote debugging found. Turn it on at "
                        "chrome://inspect/#remote-debugging, or set PCODE_BROWSER_CDP_URL."
                    )
            elif chrome_executable():
                # The port is chosen now so the session can be built before the
                # browser exists; Chrome starts on the first tool call.
                self.cdp_url = f"http://127.0.0.1:{_free_port()}"
            else:
                self.cdp_url = None
            self.session = _session_class()(
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
        self.cdp_url = None
        self.attach = False
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

    def describe(self) -> str:
        """One line for `/browser status`."""
        if not self.enabled:
            return "off"
        if self.cdp_url is None:
            how = "Playwright's Chromium (no Chrome found; Google sign-in will refuse it)"
        elif self.attached:
            how = f"attached to your Chrome at {self.cdp_url}"
        else:
            how = f"own Chrome on {self.cdp_url}, profile {profile_dir()}" + (
                "" if self.process is not None and self.process.poll() is None else " (not started)"
            )
        page = self.session.page if self.launched else None
        return f"on, {how}" + (f", at {page.url}" if page is not None else "")


STATE = BrowserState()
