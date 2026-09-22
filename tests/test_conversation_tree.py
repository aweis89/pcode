import asyncio
import json
import subprocess
import sys
from copy import deepcopy
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.messages import ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness.planning import PlanItem
from pydantic_ai_harness.step_persistence import RunRecord, ToolEffectRecord
from rich.console import Console

from pcode.app import PreviewApp
from pcode.conversation_tree import ConversationTree
from pcode.live import AgentRuntime
from pcode.runtime import PlanUpdated
from pcode.sessions import SavedSession, SessionError
from pcode.tree_ui import tree_dialog


def prompts(history):
    return [
        part.content
        for message in history
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]


async def turn(runtime, text):
    _ = [event async for event in runtime.stream(text)]
    return runtime.tree.active


@pytest.mark.parametrize("save", [False, True])
def test_branch_history_tools_plans_and_reopen(tmp_path, save):
    async def run():
        calls = []

        async def model(messages, info):
            if any(isinstance(part, ToolReturnPart) for part in messages[-1].parts):
                yield "Answer to " + prompts(messages)[-1]
            else:
                yield {0: DeltaToolCall(name="marker", json_args="{}")}

        agent = Agent(FunctionModel(stream_function=model))

        @agent.tool_plain
        def marker() -> str:
            calls.append("tool")
            return "tool result"

        saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions") if save else None
        runtime = AgentRuntime(agent, saved)
        try:
            a = await turn(runtime, "A")
            history_a = deepcopy(runtime.history)
            b = await turn(runtime, "B")
            plan = [PlanItem(id="b-plan", content="Only branch B", status="pending")]
            await runtime.plan_store.set_items(plan)
            event = PlanUpdated([item.model_dump(mode="json") for item in plan])
            if saved:
                saved.event(event)
            else:
                runtime.tree.consume({"kind": "PlanUpdated", "items": event.items})
            history_b = deepcopy(runtime.history)
            assert await runtime.navigate(a) == ""
            assert runtime.history == history_a
            assert await runtime.plan_store.get_items() == []
            c = await turn(runtime, "C")
            assert prompts(runtime.history) == ["A", "C"]
            assert runtime.tree.nodes[c].parent == a
            assert runtime.tree.nodes[b].parent == a
            assert len(calls) == 3
            await runtime.navigate(b)
            assert runtime.history == history_b
            assert await runtime.plan_store.get_items() == plan
            if saved:
                assert [
                    r["prompt"] for r in saved.transcript_records() if r["kind"] == "turn_started"
                ] == ["A", "B"]
                assert {
                    r["run_id"] for r in saved.tool_events() if r["kind"] == "turn_started"
                } == {a, b}
                assert saved.latest_plan() == event.items
                identity, root = saved.info.id, saved.directory.parent
                runtime.close()
                reopened = SavedSession.open(identity, root)
                runtime = AgentRuntime(agent, reopened)
                await runtime.restore()
                assert runtime.tree.active == b  # Cursor, not chronologically newest C.
                assert runtime.history == history_b
                assert await runtime.plan_store.get_items() == plan
                assert len(runtime.tree.nodes) == 3
            assert await runtime.navigate(b, edit=True) == "B"
            assert runtime.tree.active == a
            assert runtime.history == history_a
            assert await runtime.navigate(a, edit=True) == "A"
            assert runtime.tree.active is None
            assert runtime.history == []
            d = await turn(runtime, "D")
            assert prompts(runtime.history) == ["D"]
            assert runtime.tree.nodes[d].parent is None
            assert len(calls) == 4  # Navigation itself never runs tools.
            if save:
                await runtime.navigate(None)
                runtime.close()
                runtime = AgentRuntime(agent, SavedSession.open(identity, root))
                await runtime.restore()
                assert runtime.tree.active is None
                assert runtime.history == []
        finally:
            runtime.close()

    asyncio.run(run())


def test_legacy_journal_becomes_linear_and_interrupted_turn_falls_back(tmp_path):
    async def run():
        saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
        runtime = AgentRuntime(Agent("test"), saved)
        try:
            a = await turn(runtime, "old A")
            history = deepcopy(runtime.history)
            saved.append("turn_started", run_id="crash", prompt="old interrupted B")
            saved.append("TextDelta", text="partial answer")
            # Remove new parent fields to simulate pre-tree saved sessions.
            records = list(saved.records())
            for record in records:
                record.pop("parent_id", None)
            (saved.directory / "transcript.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records)
            )
            identity, root = saved.info.id, saved.directory.parent
            runtime.close()
            runtime = AgentRuntime(Agent("test"), SavedSession.open(identity, root))
            await runtime.restore()
            assert runtime.tree.nodes["crash"].parent == a
            assert runtime.history == history
            assert runtime.tree.nodes["crash"].status == "interrupted"
            assert await runtime.navigate("crash", edit=True) == "old interrupted B"
            assert runtime.history == history
        finally:
            runtime.close()

    asyncio.run(run())


def test_navigation_allows_interrupted_sibling_without_resolving_effects(tmp_path):
    async def run():
        saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
        runtime = AgentRuntime(Agent("test"), saved)
        try:
            a = await turn(runtime, "A")
            history = deepcopy(runtime.history)
            await saved.store.register_run(
                RunRecord(run_id="sibling", conversation_id=saved.info.id)
            )
            await saved.store.record_tool_effect(
                ToolEffectRecord(
                    run_id="sibling",
                    tool_call_id="write",
                    tool_name="write_file",
                    status="started",
                )
            )
            await runtime.navigate(None)
            assert runtime.tree.active is None
            assert runtime.history == []
            await runtime.navigate(a)
            assert runtime.history == history
            effects = await saved.store.list_unresolved_tool_effects(run_id="sibling")
            assert len(effects) == 1
            assert effects[0].status == "started"
        finally:
            runtime.close()

    asyncio.run(run())


def test_missing_checkpoint_does_not_publish_cursor(tmp_path):
    async def run():
        saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
        runtime = AgentRuntime(Agent("test"), saved)
        try:
            a = await turn(runtime, "A")
            b = await turn(runtime, "B")
            history = runtime.history
            with patch.object(saved.store, "latest_snapshot", return_value=None):
                with pytest.raises(SessionError, match="checkpoint is missing"):
                    await runtime.navigate(a)
            assert runtime.tree.active == b
            assert runtime.history is history
        finally:
            runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "keys,expected",
    [
        ("\r", ("b", False)),
        ("\x1b[A\r", ("b", True)),
        ("\x1b", None),
        ("\x03", None),
    ],
)
def test_tree_keyboard_and_active_default(keys, expected):
    async def run():
        tree = ConversationTree()
        tree.consume({"kind": "turn_started", "run_id": "a", "prompt": "Question A"})
        tree.consume({"kind": "turn_started", "run_id": "b", "prompt": "Question B"})
        with create_pipe_input() as pipe:
            dialog = tree_dialog(tree, input=pipe, output=DummyOutput())
            task = asyncio.create_task(dialog.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text(keys)
            assert await asyncio.wait_for(task, 2) == expected
            assert tree.active == "b"  # Picker has no side effects.

    asyncio.run(run())


def test_tree_browser_shows_selected_branch_and_anchors_selection():
    from pcode.tree_ui import TreeBrowser

    tree = ConversationTree()
    for identity, parent in [("a", None), ("b", "a"), ("c", "a")]:
        tree.consume(
            {
                "kind": "turn_started",
                "run_id": identity,
                "parent_id": parent,
                "prompt": "ask " + identity,
            }
        )
        tree.consume({"kind": "Message", "markdown": "Answer " + identity})
        tree.consume({"kind": "turn_completed"})
    with create_pipe_input() as pipe:
        browser = TreeBrowser(tree, input=pipe, output=DummyOutput())
        # Opens on the active row with the active branch (a → c) shown.
        assert browser.selected == ("c", False)
        assert browser.list.document.cursor_position_row == 6
        text = browser.detail.text()
        assert "ask a" in text and "Answer c" in text and "ask b" not in text
        # Selecting a's prompt keeps the active branch below it and scrolls to the prompt.
        browser.list.buffer.cursor_position = browser.list.document.translate_row_col_to_index(1, 0)
        assert browser.selected == ("a", True)
        assert browser._branch == ("a", "c")
        assert browser.detail._anchor == browser._anchors[("a", True)]
        assert browser.detail.line_offset(browser.detail._anchor, 80) == 2
        # Selecting b's response switches to the b branch.
        browser.list.buffer.cursor_position = browser.list.document.translate_row_col_to_index(4, 0)
        assert browser.selected == ("b", False)
        assert browser._branch == ("a", "b")
        assert "Answer b" in browser.detail.text() and "Answer c" not in browser.detail.text()
        lines = browser.detail.text().splitlines()
        assert "Answer b" in lines[browser.detail.line_offset(browser.detail._anchor, 80)]


def test_tree_browser_conversation_pane_highlights_the_selected_row():
    """The Conversation pane marks the Tree list's current selection, like the list itself."""
    from prompt_toolkit.filters import Always

    from pcode.tree_ui import TreeBrowser

    tree = ConversationTree()
    tree.consume({"kind": "turn_started", "run_id": "a", "parent_id": None, "prompt": "ask a"})
    tree.consume({"kind": "Message", "markdown": "Answer a"})
    tree.consume({"kind": "turn_completed"})
    with create_pipe_input() as pipe:
        browser = TreeBrowser(tree, input=pipe, output=DummyOutput())
        assert isinstance(browser.detail.window.cursorline, Always)
        # The pane always reports its scrolled-to row as the cursor, so enabling
        # cursorline highlights whatever the Tree selection scrolled to.
        browser.list.buffer.cursor_position = browser.list.document.translate_row_col_to_index(1, 0)
        assert browser.selected == ("a", True)
        row = browser.detail.line_offset(browser.detail._anchor, 80)
        content = browser.detail.control.create_content(80, 10)
        assert content.cursor_position.y == row


def test_app_navigation_and_busy_guard():
    async def run():
        runtime = AgentRuntime(Agent("test"))
        output = StringIO()
        app = PreviewApp(runtime=runtime, console=Console(file=output))
        a = await turn(runtime, "original prompt")
        app.activity.queued = 1
        with pytest.raises(ValueError, match="queued"):
            app.select_tree("")
        with pytest.raises(ValueError, match="queued"):
            await app.navigate_tree(a, edit=True)
        app.activity.queued = 0
        assert app.registry.dispatch("/tree")
        assert app.tree_requested
        assert await app.navigate_tree(a, edit=True) == "original prompt"
        assert runtime.history == []
        assert "are not undone" in " ".join(output.getvalue().split())
        assert len(runtime.tree.nodes) == 1
        runtime.reset()
        assert runtime.tree.nodes == {}

    asyncio.run(run())


def test_tree_rows_depth_first_and_branch_connectors():
    tree = ConversationTree()
    for identity, parent in [("a", None), ("b", "a"), ("c", "a"), ("d", None)]:
        tree.consume(
            {"kind": "turn_started", "run_id": identity, "parent_id": parent, "prompt": identity}
        )
        tree.consume({"kind": "Message", "markdown": "Answer " + identity})
        tree.consume({"kind": "turn_completed"})
    assert [value for value, _ in tree.rows()] == [
        (None, False),
        ("a", True),
        ("a", False),
        ("b", True),
        ("b", False),
        ("c", True),
        ("c", False),
        ("d", True),
        ("d", False),
    ]
    assert "├─ user: a" in tree.rows()[1][1]
    assert "└─ user: d" in tree.rows()[-2][1]
    assert "← active" in tree.rows()[-1][1]


def test_selected_cursor_restores_in_fresh_process(tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    runtime = AgentRuntime(Agent("test"), saved)

    async def prepare():
        a = await turn(runtime, "selected ancestor")
        await turn(runtime, "abandoned descendant")
        await runtime.navigate(a)
        return a

    try:
        active = asyncio.run(prepare())
    finally:
        runtime.close()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio, json, sys
from pathlib import Path
from pcode.sessions import SavedSession
from pydantic_ai.messages import UserPromptPart
saved = SavedSession.open(sys.argv[1], Path(sys.argv[2]))
try:
    history = asyncio.run(saved.recover())
    print(json.dumps({
        "active": saved.tree.active,
        "nodes": len(saved.tree.nodes),
        "prompts": [p.content for m in history for p in m.parts if isinstance(p, UserPromptPart)],
    }))
finally:
    saved.close()
""",
            saved.info.id,
            str(root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "active": active,
        "nodes": 2,
        "prompts": ["selected ancestor"],
    }


def test_long_linear_tree_keeps_messages_aligned():
    tree = ConversationTree()
    for i in range(100):
        tree.consume({"kind": "turn_started", "run_id": str(i), "prompt": f"Question {i}"})
    for (identity, edit), label in tree.rows()[1:]:
        assert label.startswith("user: " if edit else "assistant: ")
    assert tree.rows()[-2][1] == "user: Question 99"


def test_only_forks_add_indentation_and_keep_sibling_guides():
    tree = ConversationTree()
    for identity, parent in [
        ("a", None),
        ("b", "a"),
        ("c", "b"),
        ("d", "c"),
        ("e", "c"),
        ("f", "b"),
        ("g", "f"),
        ("h", "g"),
    ]:
        tree.consume(
            {"kind": "turn_started", "run_id": identity, "parent_id": parent, "prompt": identity}
        )
        tree.consume({"kind": "Message", "markdown": "Answer " + identity})
        tree.consume({"kind": "turn_completed"})
    assert [label for _, label in tree.rows()] == [
        "Conversation start",
        "user: a",
        "assistant: Answer a",
        "user: b",
        "assistant: Answer b",
        "├─ user: c",
        "│  assistant: Answer c",
        "│  ├─ user: d",
        "│  │  assistant: Answer d",
        "│  └─ user: e",
        "│     assistant: Answer e",
        "└─ user: f",
        "   assistant: Answer f",
        "   user: g",
        "   assistant: Answer g",
        "   user: h",
        "   assistant: Answer h ← active",
    ]


@pytest.mark.parametrize("save", [False, True])
def test_cancelled_turn_is_labelled_and_can_be_edited(tmp_path, save):
    async def run():
        async def model(messages, info):
            yield "Partial answer"
            raise asyncio.CancelledError

        saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions") if save else None
        runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)), saved)
        try:
            with pytest.raises(asyncio.CancelledError):
                await turn(runtime, "Interrupted question")
            identity = runtime.tree.active
            assert runtime.tree.nodes[identity].status == "cancelled"
            assert await runtime.navigate(identity, edit=True) == "Interrupted question"
            assert runtime.history == []
        finally:
            runtime.close()

    asyncio.run(run())
