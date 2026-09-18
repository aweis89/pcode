"""Real CPR regression: hiding the widget reclaims its entire frame."""

import pytest
from test_tmux import PLAN_SCRIPT, capture, input_rows, pane, pytestmark  # noqa: F401


@pytest.mark.parametrize("pane", [PLAN_SCRIPT], indirect=True)
@pytest.mark.parametrize("split", ["-h", "-v"])
def test_toggle_removes_entire_widget_and_restores_it(pane, split):  # noqa: F811
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "✓ Task 0")
    pane("send-keys", "-t", "preview:0.0", "C-o")
    pane("send-keys", "-t", "preview:0.0", "-l", "kept draft")
    pane("split-window", split, "-t", "preview:0.0", "cat")
    screen = capture(pane, "kept draft")
    assert "Task 0" not in screen
    assert "Tasks" not in screen and "Tools" not in screen
    assert screen.count("┌") == screen.count("└") == 1
    assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-o")
    screen = capture(pane, "✓ Task 0")
    assert "kept draft" in screen
    assert screen.count("┌") == screen.count("└") == 2
    assert input_rows(screen) == 1
