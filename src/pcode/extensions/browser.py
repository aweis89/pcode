"""A real Chrome the model can drive, with the user logging in by hand.

Off by default and per conversation: `/browser on` adds Harness's eighteen
Playwright tools plus `browser_open` and `browser_login`, and a `browser`
sub-agent that runs multi-step flows without the page text landing in the
parent's context. `/browser launch` opens the window right away; otherwise it
opens on the first browser tool call. `/browser off` closes it and removes the
tools. Nothing is persisted, so a logged-in session ends with the process at
the latest.

The browser is the user's own Chrome, started with a debugging port and a
profile of its own (see `pcode.browser` for why not Playwright's Chromium).
The window is visible on purpose: `browser_login(url)` opens a page there and
waits for the user to sign in, and the user sees what the model does with that
session afterwards. There is no sandbox around it beyond the address bar: a
page the model reads can tell it to do things with the user's login, which is
the trade-off of turning this on.

The state itself lives in `pcode.browser`, since `/reload` re-imports this file.
Copy this file to `~/.config/pcode/extensions/browser.py` to change defaults;
an empty `setup` removes the feature.
"""

import asyncio

from pcode.browser import LOGIN_TIMEOUT_SECONDS, STATE

DELEGATE_INSTRUCTIONS = (
    "A `browser` sub-agent shares the same browser window and login. Delegate to it "
    "when a browsing task takes several steps and you only need the outcome (find a "
    "value, verify a flow works, reproduce a bug), so page text stays out of this "
    "conversation. Drive the browser yourself for a single check or a screenshot, "
    "and always for `browser_login`, which needs the user's attention."
)

SUBAGENT_INSTRUCTIONS = (
    "You drive a real browser through the tools available to you and report back "
    "concisely: what you found, what worked, what failed, and the final URL. Do not "
    "narrate each step. If a page needs a login that is not present, stop and say so "
    "rather than guessing credentials; the parent will ask the user."
)


def _spawn(coroutine) -> None:
    """Run a coroutine on the terminal's loop, or inline when there is none (tests)."""
    try:
        asyncio.get_running_loop().create_task(coroutine)
    except RuntimeError:
        asyncio.run(coroutine)


async def _start(pcode) -> None:
    """Have Chrome up and the session armed, reporting a launch to the user."""
    await STATE.arm()
    if note := await STATE.ensure_chrome():
        pcode.ui.notify(note)


def _capability(pcode, toolset):
    """The browser tools, the open and login tools, and their guidance, sharing one session."""
    from pydantic_ai.capabilities import Capability
    from pydantic_ai_harness.playwright import PlaywrightBrowser

    browser_tools = set(toolset.tools) | {"browser_open", "browser_login"}

    async def browser_open() -> str:
        """Open the browser window now, without navigating anywhere.

        The window also opens on the first navigate; call this to show it to
        the user ahead of time, for example before asking them to log in.
        """
        await _start(pcode)
        await toolset.navigate("about:blank")
        page = STATE.session.page
        if page is not None:
            await page.bring_to_front()
        return "The browser window is open and in front."

    async def browser_login(url: str, done_url_prefix: str | None = None) -> str:
        """Open `url` in the visible browser window and wait for the user to log in by hand.

        Use this before pages that need an account. Returns once the user says
        they are done (`/browser done`), once the page URL starts with
        `done_url_prefix` when given, or after five minutes. The login persists
        for the rest of the conversation, so call this once per site.
        """
        await _start(pcode)
        result = await toolset.navigate(url)
        page = STATE.session.page
        if page is None:
            return str(result)
        await page.bring_to_front()
        pcode.ui.notify(
            f"Log in to {url} in the browser window, then run /browser done. "
            f"Waiting up to {LOGIN_TIMEOUT_SECONDS // 60} minutes.",
            "warning",
        )
        outcome = await STATE.wait_for_login(done_url_prefix)
        title = await page.title()
        if outcome == "timeout":
            return (
                f"Timed out waiting for the login; the page is {page.url!r} ({title!r}). "
                "Ask the user whether they finished, then continue or call browser_login again."
            )
        return f"User finished logging in. Now at {page.url!r} ({title!r})."

    class Browser(Capability):
        async def before_tool_execute(self, ctx, *, call, tool_def, args):
            # Chrome starts on the first browser tool, for the parent and the
            # sub-agent alike, on the loop the tools run on.
            if tool_def.name in browser_tools:
                await _start(pcode)
            return args

    guidance = PlaywrightBrowser(
        headless=False, block_private_addresses=False, max_content_tokens=2500
    ).get_instructions()(None)
    return Browser(
        id="browser",
        toolsets=[toolset],
        tools=[browser_open, browser_login],
        instructions=guidance,
    )


def _subagent(pcode, toolset):
    from pydantic_ai import Agent

    return Agent(
        name="browser",
        description=(
            "Drive the shared browser window through a multi-step task and report the "
            "outcome; the login the user made is already there"
        ),
        instructions=SUBAGENT_INSTRUCTIONS,
        capabilities=[_capability(pcode, toolset)],
    )


def setup(pcode) -> None:
    def turn_on() -> None:
        pcode.ui.request_reload()  # Refuses mid-turn, before anything changes.
        STATE.enabled = True
        pcode.ui.notify(
            "Browser tools on for this conversation. Pages the model reads can act on "
            "whatever you log in to there."
        )

    def browser(argument: str) -> None:
        argument = argument.strip() or "status"
        if argument == "on":
            if STATE.enabled:
                raise ValueError("The browser is already on.")
            turn_on()
        elif argument == "launch":
            if not STATE.enabled:
                turn_on()
            STATE.open()
            _spawn(_launch())
        elif argument == "off":
            if not STATE.enabled:
                raise ValueError("The browser is already off.")
            pcode.ui.request_reload()
            STATE.enabled = False
            _spawn(STATE.close())
            pcode.ui.notify("Browser closed; its tools leave on the next request.")
        elif argument == "done":
            if not STATE.launched:
                raise ValueError("No browser is open.")
            STATE.login_event().set()
            pcode.ui.notify("Login reported; the model continues.")
        else:
            pcode.ui.notify(f"Browser {STATE.describe()}. /browser on|launch|off|done.")

    async def _launch() -> None:
        try:
            await _start(pcode)
            await STATE.toolset.navigate("about:blank")
        except Exception as error:  # noqa: BLE001 - a failed launch is a notice, not a crash.
            pcode.ui.notify(f"Browser launch failed: {error}", "error")

    pcode.register_command(
        "/browser",
        "A real Chrome the model can drive; `launch` opens it, `done` after you log in",
        browser,
        arguments=("on", "launch", "off", "done", "status"),
    )
    pcode.on_close(STATE.close)
    if not STATE.enabled:
        return
    toolset = STATE.open()
    pcode.add_capability(_capability(pcode, toolset))
    pcode.instructions(DELEGATE_INSTRUCTIONS)
    pcode.subagent(_subagent(pcode, toolset), timeout_seconds=900)
