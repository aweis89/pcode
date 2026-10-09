"""`/usage`: plan limits and spend for the Claude Code and Codex logins, on demand.

Both sources are undocumented endpoints the vendors' own CLIs call, read with
the logins pcode already runs on, so no admin key is involved:

- Claude: `GET api.anthropic.com/api/oauth/usage` with Claude Code's OAuth
  token. Subscription seats get `limits` (session and weekly percentages);
  Enterprise seats billed at API rates get `spend` (used of the monthly cap).
- Codex: `GET chatgpt.com/backend-api/wham/usage` through the Codex provider's
  auth, which injects the token, refreshes it on a 401, and never writes the
  Codex CLI's `auth.json`.

Claude Code's token is read, never refreshed: its refresh tokens are single
use, so rotating one here would sign Claude Code out.
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx2

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TIMEOUT = httpx2.Timeout(15, connect=5)
KEYCHAIN_SERVICE = "Claude Code-credentials"

CLAUDE_LIMIT_LABELS = {
    "session": "Session (5h)",
    "weekly_all": "Weekly",
    "weekly_scoped": "Weekly",
}
CODEX_WINDOW_LABELS = {5 * 3600: "Session (5h)", 7 * 86400: "Weekly"}


class UsageUnavailable(Exception):
    """A provider's usage could not be read; the message says why."""


def usage_report() -> list[str]:
    """Transcript lines for every provider; one failing never hides the other."""
    lines: list[str] = []
    sources: tuple[tuple[str, Callable[[], list[str]]], ...] = (
        ("Claude Code", claude_usage),
        ("Codex", codex_usage),
    )
    for title, fetch in sources:
        try:
            body = fetch()
        except UsageUnavailable as error:
            body = [str(error)]
        lines.append(title)
        lines.extend(f"  {line}" for line in body)
    return lines


# Claude Code


def keychain_service(config_dir: str | None) -> str:
    """Claude Code's Keychain item for a config dir.

    Setting `CLAUDE_CONFIG_DIR` suffixes the name with a hash of its value,
    even when the value is the default `~/.claude`, so the unsuffixed item can
    belong to a different (often stale) login.
    """
    if not config_dir:
        return KEYCHAIN_SERVICE
    return f"{KEYCHAIN_SERVICE}-{hashlib.sha256(config_dir.encode()).hexdigest()[:8]}"


def claude_login(config_dir: str | None = None) -> dict:
    """Claude Code's `claudeAiOauth` record: the macOS Keychain, else its credentials file."""
    if config_dir is None:
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or None
    raw = ""
    if sys.platform == "darwin":
        try:
            raw = subprocess.run(
                ["security", "find-generic-password", "-s", keychain_service(config_dir), "-w"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        except OSError, subprocess.SubprocessError:
            raw = ""
    if not raw.strip():
        path = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
        try:
            raw = (path / ".credentials.json").read_text()
        except OSError:
            raw = ""
    try:
        login = json.loads(raw).get("claudeAiOauth") if raw.strip() else None
    except ValueError, AttributeError:
        login = None
    if not isinstance(login, dict) or not login.get("accessToken"):
        where = f" for {config_dir}" if config_dir else ""
        raise UsageUnavailable(f"No Claude Code login{where}; run `claude` and sign in.")
    return login


def claude_usage() -> list[str]:
    from pcode.auth import oauth_user_agent

    login = claude_login()
    expires = login.get("expiresAt")
    if isinstance(expires, int | float) and expires / 1000 < time.time():
        raise UsageUnavailable(
            "Claude Code's token has expired; run `claude` (or a claude: model) to refresh it."
        )
    try:
        response = httpx2.get(
            CLAUDE_USAGE_URL,
            timeout=TIMEOUT,
            headers={
                "Authorization": f"Bearer {login['accessToken']}",
                "anthropic-beta": "oauth-2025-04-20",
                # A generic client lands in a much stricter rate-limit bucket.
                "User-Agent": oauth_user_agent(),
            },
        )
    except httpx2.HTTPError as error:
        raise UsageUnavailable(f"Request failed: {type(error).__name__}") from None
    if response.status_code in (401, 403):
        raise UsageUnavailable("Claude Code's login was rejected; run `claude` to sign in again.")
    if response.status_code == 429:
        raise UsageUnavailable("The usage endpoint is rate limiting; try again in a minute.")
    if response.status_code != 200:
        raise UsageUnavailable(f"The usage endpoint answered HTTP {response.status_code}.")
    plan = login.get("subscriptionType")
    return ([f"Plan: {plan}"] if plan else []) + claude_lines(response.json())


def claude_lines(data: dict, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    lines = []
    limits = data.get("limits")
    if limits:
        for limit in limits:
            kind = limit.get("kind", "")
            label = CLAUDE_LIMIT_LABELS.get(kind) or kind.replace("_", " ").capitalize()
            model = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
            if model:
                label = f"{label} ({model})"
            resets = iso_time(limit.get("resets_at"))
            lines.append(percent_line(label, limit.get("percent"), resets, now))
    else:
        # Older responses carry only the windows, with no `limits` list.
        for key, label in (("five_hour", "Session (5h)"), ("seven_day", "Weekly")):
            window = data.get(key)
            if window:
                resets = iso_time(window.get("resets_at"))
                lines.append(percent_line(label, window.get("utilization"), resets, now))
    spend = data.get("spend") or {}
    used, limit = money(spend.get("used")), money(spend.get("limit"))
    if used is not None and limit is not None:
        line = f"Spend: {used} of {limit}"
        if spend.get("percent") is not None:
            line += f" ({spend['percent']:.0f}%)"
        if not spend.get("enabled"):
            reason = spend.get("disabled_reason")
            line += f", off ({reason.replace('_', ' ')})" if reason else ", off"
        lines.append(line)
    return lines or ["No limits reported for this login."]


def money(amount: dict | None) -> str | None:
    if not amount or amount.get("amount_minor") is None:
        return None
    value = amount["amount_minor"] / 10 ** amount.get("exponent", 2)
    currency = amount.get("currency", "USD")
    return f"${value:,.2f}" if currency == "USD" else f"{value:,.2f} {currency}"


# Codex


def codex_usage() -> list[str]:
    try:
        data = asyncio.run(_fetch_codex())
    except UsageUnavailable:
        raise
    except httpx2.HTTPError as error:
        raise UsageUnavailable(f"Request failed: {type(error).__name__}") from None
    except Exception as error:  # noqa: BLE001 - credential errors are the provider's own types.
        raise UsageUnavailable(str(error).split("\n")[0]) from None
    return codex_lines(data)


async def _fetch_codex() -> dict:
    from pydantic_ai.providers.openai_codex import OpenAICodexProvider

    from pcode.codex_login import credential_source

    # The same route Codex model requests take: PCODE_LLM_PROXY when set.
    proxy = os.environ.get("PCODE_LLM_PROXY", "").strip()
    options = {"proxy": proxy, "trust_env": False} if proxy else {}
    async with httpx2.AsyncClient(timeout=TIMEOUT, **options) as client:
        source = credential_source()
        # Attaches the provider's auth to `client`; without a pcode login it
        # reads the Codex CLI's auth.json read-only.
        if source is not None:
            OpenAICodexProvider(credential_source=source, http_client=client)
        else:
            OpenAICodexProvider(http_client=client)
        try:
            response = await client.get(CODEX_USAGE_URL, headers={"Accept": "application/json"})
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.ProxyError) as error:
            route = " through PCODE_LLM_PROXY" if proxy else ""
            raise UsageUnavailable(
                f"Could not reach chatgpt.com{route} ({type(error).__name__})."
            ) from None
    if response.status_code == 403 and "json" not in response.headers.get("content-type", ""):
        hint = "" if proxy else "; set PCODE_LLM_PROXY if chatgpt.com needs a proxy here"
        raise UsageUnavailable(f"chatgpt.com blocked the request (HTTP 403){hint}.")
    if response.status_code != 200:
        raise UsageUnavailable(f"The usage endpoint answered HTTP {response.status_code}.")
    return response.json()


def codex_lines(data: dict, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    lines = []
    if data.get("plan_type"):
        lines.append(f"Plan: {data['plan_type']}")
    lines.extend(codex_window_lines(data.get("rate_limit"), "", now))
    for extra in data.get("additional_rate_limits") or ():
        name = extra.get("limit_name") or extra.get("metered_feature") or "Other"
        lines.extend(codex_window_lines(extra.get("rate_limit"), f"{name} ", now))
    credits = data.get("credits") or {}
    if credits.get("unlimited"):
        lines.append("Credits: unlimited")
    elif credits.get("has_credits") and credits.get("balance") is not None:
        lines.append(f"Credits: {credits['balance']}")
    spend = data.get("spend_control") or {}
    cap = spend.get("individual_limit")
    if cap:
        line = f"Spend: {cap.get('used')} of {cap.get('limit')} ({cap.get('used_percent', 0)}%)"
        lines.append(line + (", limit reached" if spend.get("reached") else ""))
    return lines or ["No limits reported for this login."]


def codex_window_lines(details: dict | None, prefix: str, now: float) -> list[str]:
    lines = []
    for key in ("primary_window", "secondary_window"):
        window = (details or {}).get(key)
        if not window:
            continue
        seconds = window.get("limit_window_seconds") or 0
        label = CODEX_WINDOW_LABELS.get(seconds) or f"{duration(seconds)} window"
        lines.append(
            percent_line(prefix + label, window.get("used_percent"), window.get("reset_at"), now)
        )
    return lines


# Formatting


def percent_line(label: str, percent: float | None, resets: float | None, now: float) -> str:
    line = f"{label}: {percent:.0f}% used" if percent is not None else f"{label}: unknown"
    if resets is not None and resets > now:
        line += f", resets in {duration(resets - now)}"
    return line


def iso_time(value: str | None) -> float | None:
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{max(minutes, 1)}m"
