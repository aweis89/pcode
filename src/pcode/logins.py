"""Provider sign-in and sign-out for a session: `/login` and `/logout`.

`Logins` is a collaborator of `SessionController`, which holds one as
`controller.logins`. The commands only record what was asked for
(`controller.login_requested` / `logout_requested`); the controller's command
loop runs the browser flow once the handler returns.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pcode.controller import SessionController


class Logins:
    """Signs a session in to, and out of, the providers pcode can store logins for."""

    def __init__(self, controller: SessionController) -> None:
        self.controller = controller

    @property
    def view(self):
        return self.controller.view

    def login(self, argument: str) -> None:
        # Signing in does not require the conversation to already be on that
        # provider; a session on another provider keeps its own model.
        from pcode.models import login_sources

        sources = login_sources()
        current = (self.controller.model or "").partition(":")[0]
        default = current if current in sources else sources[0]
        source = argument.strip() or default
        if source not in sources:
            self.view.note(f"Usage: /login [{'|'.join(sources)}]")
            return
        self.controller.login_requested = source

    def logout(self, argument: str) -> None:
        from pcode.models import logout_sources

        sources = logout_sources()
        source = argument.strip() or sources[0]
        if source not in sources:
            self.view.note(f"Usage: /logout [{'|'.join(sources)}]")
            return
        self.controller.logout_requested = source

    async def perform_login(self, source: str | None, *, beside: bool = False) -> None:
        """Sign in; `beside` says a turn or queued prompt carries on meanwhile.

        Either way the conversation's next model request uses the new login,
        mid-turn included: a `claude:` model's CLI rereads its keychain item
        at least every 30 seconds (checked against Claude Code 2.1.300), and
        the Codex model is rebuilt here, which `FollowAgent` reads per request.
        """
        # Ctrl+C stops the work in front first; see `SessionController.cancel`.
        cancel = "Ctrl+C cancels once nothing else runs" if beside else "Ctrl+C cancels"
        if source == "openai-codex":
            await self.login_codex(cancel)
        elif source == "antigravity":
            if beside:
                self.view.warning(
                    "Wait for active work and queued messages to finish before running "
                    "/login antigravity (it opens a code-entry prompt)."
                )
                return
            await self.login_antigravity(cancel)
        else:
            await self.login_claude(cancel)

    async def login_claude(self, cancel: str = "Ctrl+C cancels") -> None:
        """Run Claude Code's own sign-in for `claude:` models, with the CLI they run."""
        from pcode.auth import LoginError
        from pcode.claude_sdk import LOGIN_ENV, MISSING_SDK, cli_path
        from pcode.claude_sdk.login import claude_login
        from pcode.models import claude_sdk_installed

        if not claude_sdk_installed():
            self.view.error(MISSING_SDK)
            return
        try:
            self.view.note(
                "Signing in to Claude Code with `claude auth login`. "
                f"Finish in the browser ({cancel})."
            )
            # Scrubbed as the requests are, so an API key cannot pass for the login.
            status = await claude_login(self.view.note, cli_path(), extra_env=LOGIN_ENV)
            plan = status.get("subscriptionType")
            self.view.note(
                "Signed in to Claude Code"
                + (f" ({plan} plan)" if plan else "")
                + ". claude: models switch to it within about 30 seconds, mid-turn included; "
                "pcode stores nothing."
            )
        except asyncio.CancelledError:
            self.view.note("Claude sign-in cancelled.")
            raise
        except LoginError as error:
            self.view.error(str(error))
        except Exception:
            self.view.error("Claude sign-in failed. No credential details were logged.")

    async def login_codex(self, cancel: str = "Ctrl+C cancels") -> None:
        from pcode.agent import codex_model
        from pcode.auth import LoginError
        from pcode.codex_login import credentials_path, login

        controller = self.controller
        self.view.note(
            "Sign in with your ChatGPT account in the browser. "
            f"If no browser opens, visit this URL ({cancel}):"
        )
        try:
            await login(notify=self.view.note)
            # Codex credentials are read when the model is built, so a Codex
            # conversation must rebuild its model to adopt the new sign-in. A
            # running turn's next request goes to it (`FollowAgent`), unless
            # the turn borrowed another model.
            codex = (controller.model or "").startswith("openai-codex:")
            if codex and hasattr(controller.runtime, "agent"):
                controller.runtime.agent.model = await asyncio.to_thread(
                    codex_model, controller.model
                )
            self.view.note(
                f"Signed in to OpenAI Codex. Credentials are stored in {credentials_path()} "
                "(owner-only) and refreshed automatically; /logout openai-codex removes them."
            )
        except asyncio.CancelledError:
            self.view.note("OpenAI Codex sign-in cancelled.")
            raise
        except LoginError as error:
            self.view.error(str(error))
        except Exception:
            self.view.error("OpenAI Codex sign-in failed. No credential details were logged.")

    async def login_antigravity(self, cancel: str = "Ctrl+C cancels") -> None:
        """Run `agy acp --login` and feed the pasted authorization code to it."""
        from pcode.antigravity import AntigravityModel
        from pcode.antigravity_login import antigravity_login
        from pcode.auth import LoginError

        seen_url = ""

        def on_notice(text: str) -> None:
            nonlocal seen_url
            if text.startswith("https://") and not seen_url:
                seen_url = text
            self.view.note(text)

        async def read_code() -> str | None:
            detail = (
                f"Finish signing in at:\n{seen_url}\n\n"
                "Then paste the authorization code or callback URL below."
                if seen_url
                else "Paste the authorization code or callback URL below."
            )
            return await self.view.read_secret("Antigravity sign-in", "Code: ", detail)

        self.view.note(
            "Signing in to Antigravity with `agy acp --login`. "
            f"Open the link in your browser ({cancel}):"
        )
        try:
            await antigravity_login(on_notice, read_code, cancel=cancel)
            model = getattr(getattr(self.controller.runtime, "agent", None), "model", None)
            if isinstance(model, AntigravityModel):
                model.reset()
            self.view.note(
                "Signed in to Antigravity. Next turn uses the new login; pcode stores nothing."
            )
        except asyncio.CancelledError:
            self.view.note("Antigravity sign-in cancelled.")
            raise
        except LoginError as error:
            self.view.error(str(error))
        except Exception:
            self.view.error("Antigravity sign-in failed. No credential details were logged.")

    async def perform_logout(self) -> None:
        from pcode.auth import LoginError
        from pcode.codex_login import credentials_path, delete_credentials

        self.controller.logout_requested = None
        try:
            removed = await asyncio.to_thread(delete_credentials, credentials_path())
        except LoginError as error:
            self.view.error(str(error))
            return
        self.view.note(
            "Removed pcode's stored OpenAI Codex login. "
            "New models fall back to the CLI login, if present; that login was not removed. "
            "The current model retains its in-memory token until it expires."
            if removed
            else "No stored pcode OpenAI Codex login to remove. CLI login is unchanged."
        )
