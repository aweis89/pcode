"""Opt-in, read-only use of pi's Anthropic credential.

Only the application reads this file at runtime. Do not inspect real credentials
when developing or testing this adapter. Pi remains the sole refresh-token owner.
"""

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from anthropic import AsyncAnthropic
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from pcode.auth import (
    OAUTH_BETAS,
    OAUTH_PREAMBLE,
    OAUTH_USER_AGENT,
    LoginError,
    SubscriptionOAuthWire,
)

# Re-exported for callers that already import the wire markers from this adapter.
__all__ = [
    "OAUTH_BETAS",
    "OAUTH_PREAMBLE",
    "OAUTH_USER_AGENT",
    "PiAnthropicModel",
    "pi_auth_path",
    "read_pi_credential",
]


@dataclass(frozen=True)
class PiCredential:
    kind: str
    value: str = field(repr=False)


def pi_auth_path() -> Path:
    directory = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    return (Path(directory).expanduser() if directory else Path.home() / ".pi" / "agent") / (
        "auth.json"
    )


def read_pi_credential(path: Path) -> PiCredential:
    """Load only the Anthropic entry; never execute pi's dynamic key expressions."""
    try:
        # Bound malformed input and avoid echoing parser exceptions or file contents.
        with path.open("r", encoding="utf-8-sig") as source:
            contents = source.read(1024 * 1024 + 1)
        if len(contents) > 1024 * 1024:
            raise ValueError
        data = json.loads(contents)
        entry = data.get("anthropic") if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            raise ValueError
        kind = entry.get("type")
        value = entry.get("access" if kind == "oauth" else "key")
        if kind not in {"oauth", "api_key"} or not isinstance(value, str) or not value:
            raise ValueError
        if any(char.isspace() for char in value) or value.startswith(("!", "$")):
            raise ValueError
        if kind == "oauth":
            expires = entry.get("expires")
            if (
                isinstance(expires, bool)
                or not isinstance(expires, (int, float))
                or not math.isfinite(expires)
            ):
                raise ValueError
            if expires <= (time.time() + 30) * 1000:
                raise LoginError(
                    "Pi's Anthropic token has expired or is about to expire. "
                    "Refresh it in pi (or run /login there), then retry in pcode."
                )
        return PiCredential(kind, value)
    except LoginError:
        raise
    except Exception:
        raise LoginError(
            "Cannot load pi's Anthropic credential. Log in to Anthropic in pi first. "
            "Expected a literal API key or OAuth entry in pi's auth.json; "
            "PI_CODING_AGENT_DIR selects a custom pi directory."
        ) from None


class PiAnthropicClient(AsyncAnthropic):
    def __init__(self, path: Path, kind: str, **kwargs):
        self._pi_path = path
        self._pi_kind = kind
        # Do not inherit API keys, bearer tokens, or base URLs from the environment.
        super().__init__(api_key="", auth_token="", base_url="https://api.anthropic.com", **kwargs)

    @property
    def auth_headers(self) -> dict[str, str]:
        # Reread on every request/retry, so pi's own refresh becomes visible.
        credential = read_pi_credential(self._pi_path)
        if credential.kind != self._pi_kind:
            raise LoginError("Pi's credential type changed. Run /login pi again or restart pcode.")
        if credential.kind == "oauth":
            return {"Authorization": f"Bearer {credential.value}"}
        return {"X-Api-Key": credential.value}


class PiAnthropicModel(SubscriptionOAuthWire, AnthropicModel):
    """Pydantic transport with the OAuth wire markers used by pi."""

    def __init__(self, model: str, *, path: Path | None = None, http_client=None):
        path = path if path is not None else pi_auth_path()
        credential = read_pi_credential(path)
        self._subscription_oauth = credential.kind == "oauth"
        client = PiAnthropicClient(path, credential.kind, http_client=http_client)
        super().__init__(
            model.removeprefix("anthropic:"),
            provider=AnthropicProvider(anthropic_client=client),
        )
