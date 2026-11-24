"""Turn the CLI's subscription rate-limit events into one-line notices.

The CLI emits a `RateLimitEvent` whenever its plan usage changes state
(`allowed` -> `allowed_warning` -> `rejected`). The limits belong to the
account, not the process, so every warm CLI can report the same change; a
notice repeats only when the status, window or reset time moves. A plain
`allowed` says nothing unless it follows a warning, when it means the window
reset.

The model layer must not import UI code, so the terminal (or host) registers a
notifier here; with none registered, events are dropped.
"""

from collections.abc import Callable
from datetime import datetime, timedelta

_notifier: Callable[[str], None] | None = None
_last: tuple | None = None

_WINDOWS = {
    "five_hour": "5-hour",
    "seven_day": "weekly",
    "seven_day_opus": "weekly Opus",
    "seven_day_sonnet": "weekly Sonnet",
    "overage": "extra usage",
}


def set_notifier(notifier: Callable[[str], None] | None) -> None:
    """Send future notices to `notifier` (the latest registration wins)."""
    global _notifier
    _notifier = notifier


def reset() -> None:
    """Forget the last notice (for tests)."""
    global _last
    _last = None


def _when(timestamp: int | None, now: datetime | None = None) -> str:
    if not timestamp:
        return ""
    at = datetime.fromtimestamp(timestamp)
    now = now or datetime.now()
    clock = f"{at:%I:%M %p}".lstrip("0")
    if at.date() == now.date():
        return f"resets {clock}"
    if at - now < timedelta(days=6):
        return f"resets {at:%a} {clock}"
    return f"resets {at:%b} {at.day} {clock}"


def describe(info, now: datetime | None = None) -> str | None:
    """The notice for a `RateLimitInfo`, or None when there is nothing to say."""
    window = _WINDOWS.get(info.rate_limit_type or "", "usage")
    resets = _when(info.resets_at, now)
    tail = f"; {resets}" if resets else ""
    if info.status == "allowed_warning":
        if isinstance(info.utilization, int | float):
            return f"Claude: {round(info.utilization * 100)}% of {window} limit used{tail}."
        return f"Claude: nearing {window} limit{tail}."
    if info.status == "rejected":
        overage = " (now using extra usage)" if info.overage_status == "allowed" else ""
        return f"Claude: {window} limit reached{overage}{tail}."
    return None


def notice(info, now: datetime | None = None) -> str | None:
    """The notice for this event if it is news, recording it as the last one."""
    global _last
    key = (info.status, info.rate_limit_type, info.resets_at)
    if key == _last:
        return None
    previous, _last = _last, key
    if info.status == "allowed":
        if previous is None or previous[0] == "allowed":
            return None
        return "Claude: usage limit reset."
    return describe(info, now)


def report(info) -> None:
    """Pass a rate-limit change to the registered notifier, if it is news."""
    if _notifier is None:
        return
    text = notice(info)
    if text:
        _notifier(text)
