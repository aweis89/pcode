"""A real Chrome the model can drive, with the user logging in by hand.

Off by default and per conversation: `/browser on` adds Harness's eighteen
Playwright tools plus `browser_open`, and a `browser` sub-agent that runs
multi-step flows without the page text landing in the parent's context.
`/browser launch` opens the window right away; otherwise it opens on the first
browser tool call. `/browser off` closes it and removes the tools.

The browser is the user's own Chrome, started with a debugging port and a
profile of its own that keeps logins between pcode sessions; `/browser attach`
joins the Chrome the user already has open instead (see `pcode.browser`).
The window is visible on purpose: a sign-in page is left to the user, who logs
in there and says so in the next message, and the user sees what the model
does with that session afterwards. There is no sandbox around it beyond the address bar: a
page the model reads can tell it to do things with the user's login, which is
the trade-off of turning this on.

The state itself lives in `pcode.browser`, since `/reload` re-imports this file.
Copy this file to `~/.config/pcode/extensions/browser.py` to change defaults;
an empty `setup` removes the feature.
"""

import asyncio

from pcode.browser import STATE

DELEGATE_INSTRUCTIONS = (
    "A `browser` sub-agent shares the same browser window and login. Delegate to it "
    "when a browsing task takes several steps and you only need the outcome (find a "
    "value, verify a flow works, reproduce a bug), so page text stays out of this "
    "conversation. Drive the browser yourself for a single check or a screenshot, "
    "and whenever a page needs the user to sign in."
)

LOGIN_INSTRUCTIONS = {
    "attach": (
        "This is the user's own Chrome: every site they are signed in to is already "
        "signed in here, so just `navigate`."
    ),
    "own": (
        "The browser profile persists between conversations, so a site the user logged "
        "in to before is still logged in; just `navigate`."
    ),
}

SIGN_IN_INSTRUCTIONS = (
    "When a page turns out to be a sign-in page, never type credentials. Call "
    "`browser_open` so the window is in front, then end your turn asking the user to "
    "log in there and tell you when they are done; continue from the same page when "
    "they do."
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

    browser_tools = set(toolset.tools) | {"browser_open"}

    async def browser_open() -> str:
        """Bring the browser window to the front, opening it first if needed.

        The current page stays as it is. Use it to show the user a page that
        needs them, such as a sign-in page, before asking them to act there.
        """
        await _start(pcode)
        if STATE.session.page is None:
            await toolset.navigate("about:blank")
        page = STATE.session.page
        if page is None:
            return "The browser could not be opened."
        await page.bring_to_front()
        return f"The browser window is in front, showing {page.url}."

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
        tools=[browser_open],
        instructions="\n".join(
            (
                guidance,
                LOGIN_INSTRUCTIONS["attach" if STATE.attach else "own"],
                SIGN_IN_INSTRUCTIONS,
            )
        ),
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
        elif argument == "attach":
            if STATE.session is not None:
                raise ValueError("A browser is already open; /browser off first.")
            STATE.attach = True
            try:
                STATE.open()
            except ValueError:
                STATE.attach = False
                raise
            if not STATE.enabled:
                turn_on()
            pcode.ui.notify(
                f"Joining your Chrome at {STATE.cdp_url}: the model can act as every account "
                "you are signed in to there. A new tab opens on first use.",
                "warning",
            )
        elif argument == "off":
            if not STATE.enabled:
                raise ValueError("The browser is already off.")
            pcode.ui.request_reload()
            STATE.enabled = False
            _spawn(STATE.close())
            pcode.ui.notify("Browser closed; its tools leave on the next request.")
        else:
            pcode.ui.notify(f"Browser {STATE.describe()}. /browser on|launch|attach|off.")

    async def _launch() -> None:
        try:
            await _start(pcode)
            await STATE.toolset.navigate("about:blank")
        except Exception as error:  # noqa: BLE001 - a failed launch is a notice, not a crash.
            pcode.ui.notify(f"Browser launch failed: {error}", "error")

    pcode.register_command(
        "/browser",
        "A real Chrome the model can drive; `launch` opens one, `attach` joins yours",
        browser,
        arguments=("on", "launch", "attach", "off", "status"),
    )
    pcode.on_close(STATE.close)
    if not STATE.enabled:
        return
    toolset = STATE.open()
    pcode.add_capability(_capability(pcode, toolset))
    pcode.instructions(DELEGATE_INSTRUCTIONS)
    pcode.subagent(_subagent(pcode, toolset), timeout_seconds=900)
