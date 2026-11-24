from datetime import datetime
from types import SimpleNamespace

import pytest
from claude_agent_sdk import RateLimitInfo

from pcode.claude_sdk import rate_limits
from pcode.controller import SessionController
from pcode.preferences import save_preferences

NOW = datetime(2026, 11, 23, 9, 0)
TODAY = int(datetime(2026, 11, 23, 15, 40).timestamp())
LATER = int(datetime(2026, 11, 26, 8, 5).timestamp())


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(rate_limits, "_notifier", None)
    monkeypatch.setattr(rate_limits, "_last", None)


def info(status, kind="five_hour", resets=TODAY, **extra) -> RateLimitInfo:
    return RateLimitInfo(status=status, rate_limit_type=kind, resets_at=resets, **extra)


def test_warning_and_rejection_name_the_window_and_reset():
    warning = info("allowed_warning", utilization=0.853)
    assert rate_limits.describe(warning, NOW) == (
        "Claude: 85% of 5-hour limit used; resets 3:40 PM."
    )
    rejected = info("rejected", "seven_day", LATER, overage_status="allowed")
    assert rate_limits.describe(rejected, NOW) == (
        "Claude: weekly limit reached (now using extra usage); resets Thu 8:05 AM."
    )
    bare = info("allowed_warning", kind=None, resets=None)
    assert rate_limits.describe(bare, NOW) == "Claude: nearing usage limit."


def test_only_changes_are_news_and_allowed_speaks_only_after_a_warning():
    assert rate_limits.notice(info("allowed"), NOW) is None
    assert rate_limits.notice(info("allowed_warning", utilization=0.9), NOW)
    # Another warm process reporting the same state says nothing.
    assert rate_limits.notice(info("allowed_warning", utilization=0.9), NOW) is None
    assert rate_limits.notice(info("rejected"), NOW)
    assert rate_limits.notice(info("allowed", resets=LATER), NOW) == "Claude: usage limit reset."


def test_report_needs_a_notifier():
    rate_limits.report(info("rejected"))  # dropped, and not remembered
    seen: list[str] = []
    rate_limits.set_notifier(seen.append)
    rate_limits.report(info("rejected"))
    assert len(seen) == 1 and seen[0].startswith("Claude: 5-hour limit reached")


def test_controller_flashes_unless_the_setting_is_off():
    flashed: list[str] = []
    controller = SimpleNamespace(
        running=True, superseded=False, view=SimpleNamespace(flash=flashed.append)
    )
    SessionController.rate_limit_notice(controller, "Claude: limit")
    save_preferences(claude_rate_limit_notices="off")
    SessionController.rate_limit_notice(controller, "Claude: again")
    assert flashed == ["Claude: limit"]
