"""Shared authentication helpers for Anthropic access.

API-key access uses ordinary Pydantic AI construction. Subscription access
(pcode's own browser sign-in) speaks to the Claude Code endpoint, which
requires the wire markers defined here.
"""

import re
import shutil
import subprocess
from functools import cache

# Compatibility markers used by Claude subscription OAuth traffic, not API keys.
OAUTH_BETAS = {"claude-code-20250219", "oauth-2025-04-20"}
OAUTH_PREAMBLE = "You are Claude Code, Anthropic's official CLI for Claude."
# The endpoint gates newer models on the advertised version: below a model's
# floor the request fails with `claude_code_version_too_old`, not a
# model-not-found. Each new model raises its floor, so prefer the locally
# installed Claude Code and keep this as the fallback for hosts without one.
OAUTH_VERSION = "2.1.280"
VERSION_TIMEOUT_SECONDS = 5.0


def _parse_version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"\d+(?:\.\d+)+", text)
    return tuple(int(part) for part in match.group().split(".")) if match else None


@cache
def oauth_user_agent() -> str:
    """Claude Code identity to advertise, never older than `OAUTH_VERSION`.

    Spoofing a version that was never released risks rejection, so the local
    install is the only source of anything newer than the pin.
    """
    version = OAUTH_VERSION
    executable = shutil.which("claude")
    if executable:
        try:
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=VERSION_TIMEOUT_SECONDS,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        else:
            local = _parse_version(result.stdout)
            if local and local > (_parse_version(OAUTH_VERSION) or ()):
                version = ".".join(str(part) for part in local)
    return f"claude-cli/{version}"


class LoginError(ValueError):
    """Safe-to-display authentication setup failure."""


class SubscriptionOAuthWire:
    """Add Claude Code wire identity to an `AnthropicModel` subclass.

    Pydantic's message mapping is a private integration seam, covered by wire
    tests. This is compatibility support, not a claim of official third-party
    OAuth support. Subclasses set `_subscription_oauth` when the resolved
    credential is an OAuth token rather than an API key.
    """

    _subscription_oauth = False

    async def _map_message(self, messages, model_request_parameters, model_settings):
        system, mapped = await super()._map_message(
            messages, model_request_parameters, model_settings
        )
        if self._subscription_oauth:
            blocks = (
                ([{"type": "text", "text": system}] if system else [])
                if isinstance(system, str)
                else list(system)
            )
            system = [{"type": "text", "text": OAUTH_PREAMBLE}, *blocks]
        return system, mapped

    def _get_betas_and_extra_headers(self, *args, **kwargs):
        betas, headers = super()._get_betas_and_extra_headers(*args, **kwargs)
        if self._subscription_oauth:
            betas.update(OAUTH_BETAS)
            headers.update({"User-Agent": oauth_user_agent(), "x-app": "cli"})
        return betas, headers


def anthropic_model(model: str, key: str, *, http_client=None):
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    # Runtime owns visible transport retries; HTTP errors must surface immediately.
    client = AsyncAnthropic(api_key=key, http_client=http_client, max_retries=0)
    return AnthropicModel(
        model.removeprefix("anthropic:"), provider=AnthropicProvider(anthropic_client=client)
    )
