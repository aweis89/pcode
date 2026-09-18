"""Visibility must reclaim real CPR-reported pane height, including after resize."""

import pytest

from test_tmux import PLAN_SCRIPT, capture, input_rows, pane, pytestmark  # noqa: F401


@pytest.mark.parametrize("pane", [PLAN_SCRIPT], indirect=True)
@pytest.mark.parametrize("split", ["-h", "-v"])
def test_toggle_tasks_reclaims_height_and_restores_latest_plan(pane, split):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "Task 8", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-o")
    pane("send-keys", "-t", "preview:0.0", "-l", "visibility draft")
    pane("split-window", split, "-t", "preview:0.0", "cat")
    screen = capture(pane, "✓ h")
    assert "Task 8" not in screen and "Tasks" not in screen
    assert screen.count("┌") == screen.count("└") == 1
    assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-o")
    screen = capture(pane, "✓ Task 0")
    assert "Tasks 12/12" in screen
    assert "visibility draft" in screen
    assert screen.count("┌") == screen.count("└") == 2
    assert input_rows(screen) == 1
