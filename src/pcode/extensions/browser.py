"""A real Chromium the model can drive, with the user logging in by hand.

Off by default and per conversation: `/browser on` adds Harness's eighteen
Playwright tools plus `browser_login`, and a `browser` sub-agent that runs
multi-step flows without the page text landing in the parent's context.
`/browser off` closes Chromium and removes the tools. Nothing is persisted, so
a logged-in session ends with the process at the latest.

The browser window is visible on purpose: `browser_login(url)` opens a page
there and waits for the user to sign in, and the user sees what the model does
with that session afterwards. There is no sandbox around it beyond the address
bar: a page the model reads can tell it to do things with the user's login,
which is the trade-off of turning this on.

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


def _capability(pcode, toolset):
    """The browser tools, the login tool, and their guidance, sharing one session."""
    from pydantic_ai.capabilities import Capability
    from pydantic_ai_harness.playwright import PlaywrightBrowser

    async def browser_login(url: str, done_url_prefix: str | None = None) -> str:
        """Open `url` in the visible browser window and wait for the user to log in by hand.

        Use this before pages that need an account. Returns once the user says
        they are done (`/browser done`), once the page URL starts with
        `done_url_prefix` when given, or after five minutes. The login persists
        for the rest of the conversation, so call this once per site.
        """
        await STATE.arm()
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
        async def before_run(self, ctx) -> None:
            # Runs for the parent and the sub-agent alike, on the tools' loop.
            await STATE.arm()

    guidance = PlaywrightBrowser(
        headless=False, block_private_addresses=False, max_content_tokens=2500
    ).get_instructions()(None)
    return Browser(
        id="browser",
        toolsets=[toolset],
        tools=[browser_login],
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
    def browser(argument: str) -> None:
        argument = argument.strip() or "status"
        if argument == "on":
            if STATE.enabled:
                raise ValueError("The browser is already on.")
            pcode.ui.request_reload()  # Refuses mid-turn, before anything changes.
            STATE.enabled = True
            pcode.ui.notify(
                "Browser tools on for this conversation. Chromium opens on first use "
                "(downloaded first if missing). Pages the model reads can act on your logins."
            )
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
            state = "on" if STATE.enabled else "off"
            page = STATE.session.page if STATE.launched else None
            where = f", at {page.url}" if page is not None else ""
            pcode.ui.notify(f"Browser {state}{where}. /browser on|off|done.")

    pcode.register_command(
        "/browser",
        "Toggle a real browser the model can drive; `done` after you log in",
        browser,
        arguments=("on", "off", "done", "status"),
    )
    pcode.on_close(STATE.close)
    if not STATE.enabled:
        return
    toolset = STATE.open()
    pcode.add_capability(_capability(pcode, toolset))
    pcode.instructions(DELEGATE_INSTRUCTIONS)
    pcode.subagent(_subagent(pcode, toolset), timeout_seconds=900)
