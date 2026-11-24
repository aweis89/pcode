"""Turn the Claude Code CLI's plan rate-limit events into one-line notices.

`claude:` sessions (`pcode.claude_sdk.session`) pass each `RateLimitEvent`'s
info here; the controller registers the notifier that flashes it. This module
sits outside `pcode.claude_sdk` and imports nothing heavy because the
controller loads it at startup, before the agent stack (tests/test_startup.py).

The limits are the account's, not a process's, so every warm CLI can report
the same change. State is kept per window (5-hour, weekly, ...): a window's
notice repeats only when its status or reset time moves, and a plain `allowed`
speaks only after that window warned, when it means the window reset.
"""

import weakref
from collections.abc import Callable
from datetime import datetime, timedelta
from types import MethodType

# A weak reference, so a controller that was replaced (/attach, /detach) is
# not kept alive by having registered.
_notifier: Callable[[], Callable[[str], None] | None] | None = None
_last: dict[str | None, tuple[str, int | None]] = {}

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
    if notifier is None:
        _notifier = None
    elif isinstance(notifier, MethodType):
        _notifier = weakref.WeakMethod(notifier)
    else:
        _notifier = lambda: notifier  # noqa: E731


def reset() -> None:
    """Forget the notifier and every window's state (for tests)."""
    global _notifier
    _notifier = None
    _last.clear()


def _when(timestamp: int | None, now: datetime) -> str:
    if not timestamp:
        return ""
    at = datetime.fromtimestamp(timestamp)
    if at <= now:
        return ""
    clock = f"{at:%I:%M %p}".lstrip("0")
    if at.date() == now.date():
        return f"resets {clock}"
    if at - now < timedelta(days=6):
        return f"resets {at:%a} {clock}"
    return f"resets {at:%b} {at.day} {clock}"


def describe(info, now: datetime | None = None) -> str | None:
    """The notice for a `RateLimitInfo`, or None when there is nothing to say."""
    window = _WINDOWS.get(info.rate_limit_type or "", "usage")
    resets = _when(info.resets_at, now or datetime.now())
    tail = f"; {resets}" if resets else ""
    if info.status == "allowed_warning":
        if info.utilization is not None:
            return f"Claude: {round(info.utilization * 100)}% of {window} limit used{tail}."
        return f"Claude: nearing {window} limit{tail}."
    if info.status == "rejected":
        overage = " (now using extra usage)" if info.overage_status == "allowed" else ""
        return f"Claude: {window} limit reached{overage}{tail}."
    return None


def notice(info, now: datetime | None = None) -> str | None:
    """The notice for this event if it is news for its window, recording it."""
    window = info.rate_limit_type
    state = (info.status, info.resets_at)
    previous = _last.get(window)
    if state == previous:
        return None
    _last[window] = state
    if info.status == "allowed":
        if previous is None or previous[0] == "allowed":
            return None
        return f"Claude: {_WINDOWS.get(window or '', 'usage')} limit reset."
    return describe(info, now)


def report(info) -> None:
    """Pass a rate-limit change to the registered notifier, if it is news."""
    notifier = _notifier() if _notifier is not None else None
    if notifier is None:
        return
    text = notice(info)
    if text:
        notifier(text)
