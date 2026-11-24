import gc
from datetime import datetime
from types import SimpleNamespace

from claude_agent_sdk import RateLimitInfo

from pcode import claude_notices
from pcode.controller import SessionController
from pcode.preferences import save_preferences

NOW = datetime(2026, 11, 23, 9, 0)
TODAY = int(datetime(2026, 11, 23, 15, 40).timestamp())
LATER = int(datetime(2026, 11, 26, 8, 5).timestamp())
PAST = int(datetime(2026, 11, 23, 8, 0).timestamp())


def info(status, kind="five_hour", resets=TODAY, **extra) -> RateLimitInfo:
    return RateLimitInfo(status=status, rate_limit_type=kind, resets_at=resets, **extra)


def test_warning_and_rejection_name_the_window_and_reset():
    warning = info("allowed_warning", utilization=0.853)
    assert claude_notices.describe(warning, NOW) == (
        "Claude: 85% of 5-hour limit used; resets 3:40 PM."
    )
    rejected = info("rejected", "seven_day", LATER, overage_status="allowed")
    assert claude_notices.describe(rejected, NOW) == (
        "Claude: weekly limit reached (now using extra usage); resets Thu 8:05 AM."
    )
    bare = info("allowed_warning", kind=None, resets=None)
    assert claude_notices.describe(bare, NOW) == "Claude: nearing usage limit."
    # A reset time already past says nothing about when.
    stale = info("allowed_warning", resets=PAST)
    assert claude_notices.describe(stale, NOW) == "Claude: nearing 5-hour limit."


def test_only_changes_are_news_and_allowed_speaks_only_after_a_warning():
    assert claude_notices.notice(info("allowed"), NOW) is None
    assert claude_notices.notice(info("allowed_warning", utilization=0.9), NOW)
    # Another warm process reporting the same state says nothing.
    assert claude_notices.notice(info("allowed_warning", utilization=0.9), NOW) is None
    assert claude_notices.notice(info("rejected"), NOW)
    assert claude_notices.notice(info("allowed", resets=LATER), NOW) == (
        "Claude: 5-hour limit reset."
    )


def test_windows_are_tracked_separately():
    assert claude_notices.notice(info("allowed_warning"), NOW)
    # The weekly window being fine is not the 5-hour one resetting.
    assert claude_notices.notice(info("allowed", "seven_day", LATER), NOW) is None
    # Processes reporting alternate windows do not repeat the warning.
    assert claude_notices.notice(info("allowed_warning"), NOW) is None
    assert claude_notices.notice(info("allowed", "seven_day", LATER), NOW) is None


def test_report_needs_a_notifier_and_holds_controllers_weakly():
    claude_notices.report(info("rejected"))  # dropped, and not remembered
    seen: list[str] = []
    claude_notices.set_notifier(seen.append)
    claude_notices.report(info("rejected"))
    assert len(seen) == 1 and seen[0].startswith("Claude: 5-hour limit reached")

    class Owner:
        def notify(self, text):
            seen.append(text)

    owner = Owner()
    claude_notices.set_notifier(owner.notify)
    del owner
    gc.collect()
    claude_notices.report(info("allowed_warning", "seven_day"))
    assert len(seen) == 1


def test_controller_flashes_unless_the_setting_is_off():
    flashed: list[str] = []
    controller = SimpleNamespace(
        running=True, superseded=False, view=SimpleNamespace(flash=flashed.append)
    )
    SessionController.rate_limit_notice(controller, "Claude: limit")
    save_preferences(claude_rate_limit_notices="off")
    SessionController.rate_limit_notice(controller, "Claude: again")
    assert flashed == ["Claude: limit"]
