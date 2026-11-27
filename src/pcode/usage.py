"""`/usage`: plan limits and spend for the Claude Code, Codex, and Antigravity logins.

All three come from the logins pcode already runs on, so no admin key is involved:

- Claude: Claude Code's own `/usage`, run headless (`claude -p /usage`). The
  CLI answers from its own login; pcode never reads Claude Code's token.
- Codex: `GET chatgpt.com/backend-api/wham/usage`, an undocumented endpoint the
  Codex CLI calls, through the Codex provider's auth, which injects the token,
  refreshes it on a 401, and never writes the Codex CLI's `auth.json`.
- Antigravity: `agy -p /usage --output-format json`, run headless. The CLI
  answers from its own login without a model turn.
"""

import asyncio
import json
import math
import os
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime

import httpx2

USAGE_JSON_COMMAND = "--usage-json"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TIMEOUT = httpx2.Timeout(15, connect=5)
CLAUDE_USAGE_ARGS = ("-p", "/usage", "--output-format", "json", "--no-session-persistence")
CLAUDE_USAGE_TIMEOUT = 30
ANTIGRAVITY_USAGE_ARGS = ("-p", "/usage", "--output-format", "json")
ANTIGRAVITY_USAGE_TIMEOUT = 15
ANTIGRAVITY_WINDOW_LABELS = {"5h": "5h", "weekly": "Weekly"}
# The CLI's report continues with a local breakdown of what drove usage.
CLAUDE_BREAKDOWN_HEADING = "What's contributing"
CODEX_WINDOW_LABELS = {5 * 3600: "Session (5h)", 7 * 86400: "Weekly"}
USAGE_PROVIDERS = ("claude", "openai-codex", "antigravity")


class UsageUnavailable(Exception):
    """A provider's usage could not be read; the message says why."""


def usage_provider_for(model: str | None) -> str | None:
    """The `/usage` provider for `model`, or None if it is not a subscription provider."""
    provider = (model or "").partition(":")[0]
    return provider if provider in USAGE_PROVIDERS else None


def usage_report(provider: str | None = None) -> list[str]:
    """Transcript lines for one provider, or all of them; one failing never hides another."""
    lines: list[str] = []
    all_sources: tuple[tuple[str, str, Callable[[], list[str]]], ...] = (
        ("claude", "Claude Code", claude_usage),
        ("openai-codex", "Codex", codex_usage),
        ("antigravity", "Antigravity", antigravity_usage),
    )
    if provider is None or provider == "all":
        sources = all_sources
    elif provider in USAGE_PROVIDERS:
        sources = tuple(entry for entry in all_sources if entry[0] == provider)
    else:
        raise ValueError(f"Unknown usage provider: {provider!r}")
    for _key, title, fetch in sources:
        try:
            body = fetch()
        except UsageUnavailable as error:
            body = [str(error)]
        lines.append(title)
        lines.extend(f"  {line}" for line in body)
    return lines


# Claude Code

_CLAUDE_SESSION_RE = re.compile(r"^current session:\s*(\d+(?:\.\d+)?)%\s*used\b", re.IGNORECASE)
_CLAUDE_WEEKLY_ALL_RE = re.compile(
    r"^current week\s*\(all models\):\s*(\d+(?:\.\d+)?)%\s*used\b", re.IGNORECASE
)
_CLAUDE_WEEKLY_ANY_RE = re.compile(
    r"^current week(?:\s*\([^)]*\))?:\s*(\d+(?:\.\d+)?)%\s*used\b", re.IGNORECASE
)


def _fetch_claude_report() -> str:
    """Run `claude -p /usage` headless and return its raw report text."""
    from pcode.models import claude_sdk_installed

    if not claude_sdk_installed():
        from pcode.claude_sdk import MISSING_SDK

        raise UsageUnavailable(MISSING_SDK)
    from pcode.claude_sdk import LOGIN_ENV, cli_path

    executable = cli_path()
    if executable is None:
        raise UsageUnavailable("Claude Code (`claude`) was not found.")
    try:
        result = subprocess.run(
            # No setting sources: a user or project hook has no business here.
            [executable, *CLAUDE_USAGE_ARGS, "--setting-sources", ""],
            capture_output=True,
            text=True,
            timeout=CLAUDE_USAGE_TIMEOUT,
            stdin=subprocess.DEVNULL,
            cwd=tempfile.gettempdir(),
            # Scrubbed as `claude:` requests are, so an API key cannot answer for the
            # plan; and a `claude` found on PATH must not update itself mid-command.
            env={**os.environ, **LOGIN_ENV, "DISABLE_AUTOUPDATER": "1"},
        )
    except subprocess.TimeoutExpired:
        raise UsageUnavailable("Claude Code did not answer in time.") from None
    except OSError as error:
        raise UsageUnavailable(f"Could not run Claude Code ({type(error).__name__}).") from None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("result"), str):
        detail = (result.stderr or "").strip().splitlines()[:1]
        reason = f": {detail[0]}" if detail else ""
        raise UsageUnavailable(f"Claude Code's /usage failed (exit {result.returncode}){reason}.")
    if data.get("is_error"):
        first = data["result"].strip().splitlines()[:1]
        raise UsageUnavailable(first[0] if first else "Claude Code's /usage failed.")
    return data["result"]


def claude_usage() -> list[str]:
    """The plan limits Claude Code's own `/usage` reports, run headless.

    `claude -p /usage` answers locally from the CLI's login without a model
    turn, so pcode never reads Claude Code's token.
    """
    return claude_lines(_fetch_claude_report())


def claude_summary(report: str) -> dict:
    """Structured session/weekly percentage summary from Claude Code's `/usage` text."""
    if report.lstrip().startswith("Total cost:"):
        return {"provider": "claude", "windows": []}
    section = report.split(CLAUDE_BREAKDOWN_HEADING, 1)[0]
    session_pct: int | None = None
    weekly_all_pct: int | None = None
    weekly_any_pct: int | None = None
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if session_pct is None and (match := _CLAUDE_SESSION_RE.match(line)):
            session_pct = _clamp_percent(float(match.group(1)))
        elif weekly_all_pct is None and (match := _CLAUDE_WEEKLY_ALL_RE.match(line)):
            weekly_all_pct = _clamp_percent(float(match.group(1)))
        elif weekly_any_pct is None and (match := _CLAUDE_WEEKLY_ANY_RE.match(line)):
            weekly_any_pct = _clamp_percent(float(match.group(1)))
    windows: list[dict] = []
    if session_pct is not None:
        windows.append({"key": "5h", "label": "5h", "used_percent": session_pct})
    weekly_pct = weekly_all_pct if weekly_all_pct is not None else weekly_any_pct
    if weekly_pct is not None:
        windows.append({"key": "weekly", "label": "wk", "used_percent": weekly_pct})
    return {"provider": "claude", "windows": windows}


def claude_usage_summary() -> dict:
    return claude_summary(_fetch_claude_report())


def claude_lines(report: str) -> list[str]:
    """The limits part of the CLI's report, without its local usage breakdown."""
    # Without a subscription login the CLI reports this session's cost instead.
    if report.lstrip().startswith("Total cost:"):
        return ["No Claude subscription login; run /login claude."]
    report = report.split(CLAUDE_BREAKDOWN_HEADING, 1)[0]
    lines = [line.strip() for line in report.splitlines() if line.strip()]
    return lines or ["No limits reported for this login."]


# Codex


def codex_usage() -> list[str]:
    return codex_lines(_fetch_codex_sync())


def _fetch_codex_sync() -> dict:
    try:
        return asyncio.run(_fetch_codex())
    except UsageUnavailable:
        raise
    except httpx2.HTTPError as error:
        raise UsageUnavailable(f"Request failed: {type(error).__name__}") from None
    except Exception as error:  # noqa: BLE001 - credential errors are the provider's own types.
        raise UsageUnavailable(str(error).split("\n")[0]) from None


def codex_summary(data: dict) -> dict:
    """Structured primary/secondary window summary from Codex's usage response."""
    details = data.get("rate_limit")
    windows: list[dict] = []
    if isinstance(details, dict):
        for key in ("primary_window", "secondary_window"):
            window = details.get(key)
            if not isinstance(window, dict):
                continue
            used = window.get("used_percent")
            if (
                not isinstance(used, (int, float))
                or isinstance(used, bool)
                or not math.isfinite(used)
            ):
                continue
            seconds = window.get("limit_window_seconds")
            valid_seconds = (
                isinstance(seconds, (int, float))
                and not isinstance(seconds, bool)
                and math.isfinite(seconds)
                and seconds > 0
            )
            if valid_seconds and seconds == 5 * 3600:
                win_key, label = "5h", "5h"
            elif valid_seconds and seconds == 7 * 86400:
                win_key, label = "weekly", "wk"
            elif valid_seconds:
                win_key = label = duration(seconds)
            elif key == "primary_window":
                win_key, label = "5h", "5h"
            else:
                win_key, label = "weekly", "wk"
            windows.append({"key": win_key, "label": label, "used_percent": _clamp_percent(used)})
    return {"provider": "openai-codex", "windows": windows}


def codex_usage_summary() -> dict:
    return codex_summary(_fetch_codex_sync())


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


# Antigravity


def _run_antigravity_usage(
    executable: str, env: dict[str, str], timeout: float
) -> tuple[int, str, str]:
    """Run `agy -p /usage --output-format json`, bailing early if it asks to log in."""
    process = subprocess.Popen(
        [executable, *ANTIGRAVITY_USAGE_ARGS],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tempfile.gettempdir(),
        env=env,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    selector = selectors.DefaultSelector()
    needle = b"authentication required"
    stderr_tail = b""
    deadline = time.monotonic() + timeout
    try:
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(executable, timeout)
            events = selector.select(timeout=remaining)
            if not events:
                raise subprocess.TimeoutExpired(executable, timeout)
            for key, _ in events:
                try:
                    chunk = os.read(key.fd, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    stdout_chunks.append(chunk)
                else:
                    stderr_chunks.append(chunk)
                    # Without a login, `agy -p` prints an auth URL and waits 60s on stdin.
                    window = stderr_tail + chunk.lower()
                    if needle in window:
                        raise UsageUnavailable("No Antigravity login; run /login antigravity.")
                    stderr_tail = window[-(len(needle) - 1) :]
        remaining = max(0.1, deadline - time.monotonic())
        returncode = process.wait(timeout=remaining)
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    stdout = b"".join(stdout_chunks).decode("utf-8", "replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", "replace")
    return returncode, stdout, stderr


def _fetch_antigravity_data() -> dict:
    """Run `agy -p /usage --output-format json` and return the parsed payload."""
    from pcode.antigravity import COMMAND, child_env

    executable = shutil.which(COMMAND)
    if executable is None:
        raise UsageUnavailable(f"Antigravity CLI (`{COMMAND}`) was not found.")
    try:
        returncode, stdout, stderr = _run_antigravity_usage(
            executable, child_env(), ANTIGRAVITY_USAGE_TIMEOUT
        )
    except UsageUnavailable:
        raise
    except subprocess.TimeoutExpired:
        raise UsageUnavailable("Antigravity CLI did not answer in time.") from None
    except OSError as error:
        raise UsageUnavailable(f"Could not run Antigravity CLI ({type(error).__name__}).") from None
    try:
        data = json.loads(stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict) or returncode != 0:
        detail = stderr.strip().splitlines()[:1]
        reason = f": {detail[0]}" if detail else ""
        raise UsageUnavailable(f"Antigravity's /usage failed (exit {returncode}){reason}.")
    status = data.get("status")
    if isinstance(status, str) and status.upper() not in ("", "SUCCESS"):
        raise UsageUnavailable(f"Antigravity's /usage failed ({status}).")
    return data


def antigravity_usage() -> list[str]:
    """The quota limits `agy -p /usage --output-format json` reports."""
    return antigravity_lines(_fetch_antigravity_data())


_WINDOW_ORDER = {"5h": 0, "weekly": 1}


def antigravity_summary(data: dict) -> dict:
    """Structured per-group quota summary from `agy -p /usage --output-format json`."""
    command = data.get("command")
    raw_groups = (
        command.get("data", {}).get("groups")
        if isinstance(command, dict) and isinstance(command.get("data"), dict)
        else None
    )
    groups: list[dict] = []
    if isinstance(raw_groups, list):
        for group in raw_groups:
            if not isinstance(group, dict):
                continue
            group_name = str(group.get("name") or "Models")
            windows: list[dict] = []
            for bucket in group.get("buckets") or ():
                if not isinstance(bucket, dict):
                    continue
                remaining = bucket.get("remaining_fraction")
                if (
                    not isinstance(remaining, (int, float))
                    or isinstance(remaining, bool)
                    or not math.isfinite(remaining)
                ):
                    continue
                window = bucket.get("window")
                window_str = window if isinstance(window, str) else ""
                if window_str == "5h":
                    win_key, label = "5h", "5h"
                elif window_str == "weekly":
                    win_key, label = "weekly", "wk"
                else:
                    fallback = window_str or str(bucket.get("name") or "limit")
                    win_key, label = fallback, fallback
                used_pct = _clamp_percent((1.0 - float(remaining)) * 100.0)
                windows.append({"key": win_key, "label": label, "used_percent": used_pct})
            windows.sort(key=lambda item: _WINDOW_ORDER.get(item["key"], 99))
            if windows:
                groups.append({"name": group_name, "windows": windows})
    default_windows = groups[0]["windows"] if groups else []
    return {"provider": "antigravity", "windows": default_windows, "groups": groups}


def antigravity_usage_summary() -> dict:
    return antigravity_summary(_fetch_antigravity_data())


def _iso_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def antigravity_lines(data: dict, now: float | None = None) -> list[str]:
    """Format the quota groups from `agy -p /usage --output-format json`."""
    now = time.time() if now is None else now
    command = data.get("command")
    groups = (
        command.get("data", {}).get("groups")
        if isinstance(command, dict) and isinstance(command.get("data"), dict)
        else None
    )
    lines: list[str] = []
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_name = group.get("name") or "Models"
            for bucket in group.get("buckets") or ():
                if not isinstance(bucket, dict):
                    continue
                window = bucket.get("window")
                window_str = window if isinstance(window, str) else None
                window_label = (
                    (ANTIGRAVITY_WINDOW_LABELS.get(window_str) if window_str else None)
                    or bucket.get("name")
                    or window_str
                    or "Limit"
                )
                remaining = bucket.get("remaining_fraction")
                used = (
                    max(0.0, min(100.0, (1.0 - float(remaining)) * 100.0))
                    if isinstance(remaining, (int, float))
                    and not isinstance(remaining, bool)
                    and math.isfinite(remaining)
                    else None
                )
                resets = _iso_timestamp(bucket.get("reset_time"))
                lines.append(percent_line(f"{group_name} ({window_label})", used, resets, now))
    if lines:
        return lines
    response = data.get("response")
    if isinstance(response, str):
        fallback = [line.strip() for line in response.splitlines() if line.strip()]
        if fallback:
            return fallback
    return ["No limits reported for this login."]


def _clamp_percent(value: float) -> int:
    return round(max(0.0, min(100.0, float(value))))


def usage_summary(provider: str) -> dict:
    """Structured percentage summary for one subscription provider."""
    sources: dict[str, Callable[[], dict]] = {
        "claude": claude_usage_summary,
        "openai-codex": codex_usage_summary,
        "antigravity": antigravity_usage_summary,
    }
    fetch = sources.get(provider)
    if fetch is None:
        raise ValueError(f"Unknown usage provider: {provider!r}")
    return fetch()


def print_usage_json(argv: list[str]) -> int:
    """Fast-path CLI entry for `pcode --usage-json <provider>`."""
    provider = argv[0].strip() if len(argv) == 1 else ""
    if provider not in USAGE_PROVIDERS:
        print(f"Usage: pcode {USAGE_JSON_COMMAND} [{'|'.join(USAGE_PROVIDERS)}]", file=sys.stderr)
        return 2
    try:
        payload = usage_summary(provider)
    except UsageUnavailable as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(payload, separators=(",", ":")))
    return 0


# Formatting


def percent_line(label: str, percent: float | None, resets: float | None, now: float) -> str:
    line = f"{label}: {percent:.0f}% used" if percent is not None else f"{label}: unknown"
    if resets is not None and resets > now:
        line += f", resets in {duration(resets - now)}"
    return line


def duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{max(minutes, 1)}m"
