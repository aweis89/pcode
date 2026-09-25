"""Tree guides describe the visible task/delegate/tool hierarchy, not hidden rows."""

from functools import partial

import pytest
from rich.cells import cell_len

from pcode.runtime import ToolStarted
from pcode.tool_panel import ToolCall, ToolHistory, panel_fragments, task_panel_rows


@pytest.fixture
def task_tree(monkeypatch):
    monkeypatch.setattr("pcode.tool_panel.monotonic", lambda: 10.0)
    monkeypatch.setattr("pcode.tool_panel.ToolCall", partial(ToolCall, started=10.0))
    history = ToolHistory()
    history.record(
        ToolStarted(
            "delegate_task", "", "worker", agent="worker", task="Fix it", activity="Working"
        )
    )
    history.record_plan(
        "worker",
        [
            {"content": "Read the code", "status": "in_progress"},
            {"content": "Test the fix", "status": "pending"},
        ],
    )
    history.record(ToolStarted("read_file", "child.py", "worker:read", parent_call_id="worker"))
    history.record(ToolStarted("run_command", "check", "check", command="make check"))
    history.record(ToolStarted("grep", "status row", "status"))
    items = [
        {"content": "Inspect", "status": "completed"},
        {"content": "Implement", "status": "in_progress"},
        {"content": "Validate", "status": "pending"},
    ]
    return items, history


def test_guides_continue_past_descendants_to_the_next_visible_sibling(task_tree):
    items, history = task_tree
    rows = task_panel_rows(items, history, 10, "*")
    assert rows == [
        ("class:plan", "✓ Inspect"),
        ("class:plan.active", "* Implement"),
        ("class:plan.agent", "├── ✦ Worker · 0.0s · Working · Fix it"),
        ("class:plan.active", "│   ├── * Read the code"),
        ("class:plan.active", "│   │   └── ⟳ Read · 0.0s · child.py"),
        ("class:plan", "│   └── ○ Test the fix"),
        ("class:plan.active", "└── ⟳ Run · 0.0s · make check"),
        ("class:plan", "○ Validate"),
    ]


@pytest.mark.parametrize("budget", range(11))
def test_clipped_tree_keeps_ancestors_and_ends_at_the_last_visible_sibling(task_tree, budget):
    items, history = task_tree
    rows = task_panel_rows(items, history, budget, "*")
    assert len(rows) <= budget
    text = [text for _, text in rows]
    if budget == 0:
        assert rows == []
    elif budget == 1:
        assert text == ["* Implement"]
    else:
        assert "* Implement" in text
        delegate = next(line for line in text if "✦ Worker" in line)
        if budget < 6:
            assert delegate.startswith("└── ")
            assert not any("make check" in line for line in text)
        else:
            assert delegate.startswith("├── ")
            assert "└── ⟳ Run · 0.0s · make check" in text
        if budget == 3:
            assert text[-1] == "    └── * Read the code"
        if budget == 4:
            assert text[-2:] == ["    ├── * Read the code", "    └── ○ Test the fix"]
        if budget == 5:
            assert text[-3:] == [
                "    ├── * Read the code",
                "    │   └── ⟳ Read · 0.0s · child.py",
                "    └── ○ Test the fix",
            ]


def test_parallel_delegates_have_separate_branches(task_tree):
    _, history = task_tree
    history.record(ToolStarted("delegate_task", "", "reviewer", agent="reviewer", task="Review"))
    history.record_plan("reviewer", [{"content": "Check diff", "status": "pending"}])
    history.record(ToolStarted("read_file", "diff", "reviewer:read", parent_call_id="reviewer"))
    # Keep the child visible instead of giving it the status row.
    history.record(ToolStarted("grep", "new status row", "new-status"))
    text = [text for _, text in history.rows(10, nested=True)]
    assert text == [
        "├── ✦ Worker · 0.0s · Working · Fix it",
        "│   ├── ⟳ Read the code",
        "│   │   └── ⟳ Read · 0.0s · child.py",
        "│   └── ○ Test the fix",
        "├── ✦ Reviewer · 0.0s · Starting · Review",
        "│   ├── ○ Check diff",
        "│   └── ⟳ Read · 0.0s · diff",
        "├── ⟳ Run · 0.0s · make check",
        "└── ⟳ Search · 0.0s · status row",
    ]


def test_without_an_active_task_only_the_delegate_children_have_guides(task_tree):
    items, history = task_tree
    items[1]["status"] = "completed"
    rows = task_panel_rows(items, history, 10, "*")
    assert [text for _, text in rows] == [
        "✓ Inspect",
        "✓ Implement",
        "○ Validate",
        "✦ Worker · 0.0s · Working · Fix it",
        "├── * Read the code",
        "│   └── ⟳ Read · 0.0s · child.py",
        "└── ○ Test the fix",
        "⟳ Run · 0.0s · make check",
    ]
    assert task_panel_rows([], history, 10, "*") == rows[3:]


@pytest.mark.parametrize("width", [1, 4, 8, 12, 40, 80])
def test_nested_guides_and_wide_text_share_the_terminal_cell_budget(task_tree, width):
    items, history = task_tree
    history.plans["worker"][0]["content"] = "界e\u0301🙂\n\x1b[2J" * 10
    rows = task_panel_rows(items, history, 10, "*")
    fragments = panel_fragments(rows, width)
    lines = "".join(text for _, text in fragments).splitlines()
    assert len(lines) == len(rows)
    assert all(cell_len(line) <= width for line in lines)
    assert "\x1b" not in "".join(lines)
