import asyncio
from io import StringIO
from unittest.mock import Mock, patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from rich.console import Console
from rich.text import Text

from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.session_ui import SessionBrowser, literal, session_info_dialog
from pcode.sessions import SavedSession, SessionError, Turn, first_prompt, session_turns


def two_sessions(tmp_path):
    """Newest-first: `older` in this workspace, `newer` in another, `newest` here."""
    from pcode.runtime import Message

    root = tmp_path / "sessions"
    ids = {}
    for name, workspace, prompts in [
        ("older", tmp_path, [("Fix the cache warning", "Patched cache_warnings.py")]),
        ("other", tmp_path / "elsewhere", [("Cache question elsewhere", "")]),
        ("newest", tmp_path, [("Add a theme", "Done"), ("Now tests", "Wrote tests for the theme")]),
    ]:
        saved = SavedSession.create("test:local", workspace, root)
        for prompt, response in prompts:
            saved.append("turn_started", prompt=prompt)
            if response:
                saved.event(Message(response))
            saved.append("turn_completed")
        saved.save_info()
        saved.close()
        ids[name] = saved.info.id
    return root, ids


def browser(tmp_path, **options):
    from pcode.sessions import list_sessions

    root, ids = two_sessions(tmp_path)
    records = list_sessions(root)
    assert [info.id for info in records] == [ids["newest"], ids["other"], ids["older"]]
    return SessionBrowser(
        records, root=root, workspace=tmp_path, output=DummyOutput(), **options
    ), ids


@pytest.mark.parametrize(
    "keys,expected",
    [
        ("\r", "newest"),
        ("\x1b[B\r", "older"),
        ("\x1b", None),
        # Search narrows the list to the matching session; Enter resumes it while typing.
        ("/cache warn\r", "older"),
        # Arrows steer the list while typing; Enter does nothing with no match.
        ("/a\x1b[B\r", "older"),
        ("/nothing-matches\r\x1b", None),
    ],
)
def test_browser_keyboard(tmp_path, keys, expected):
    async def run():
        with create_pipe_input() as pipe:
            app, ids = browser(tmp_path, input=pipe)
            assert app.app.mouse_support()
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.05)
            pipe.send_text(keys)
            result = await asyncio.wait_for(task, 2)
            assert result == (ids[expected] if expected else None)

    asyncio.run(run())


def test_browser_scopes_searches_and_shows_turns(tmp_path):
    with create_pipe_input() as pipe:
        app, ids = browser(tmp_path, input=pipe)
        # Only this workspace, newest first; the detail pane lists every turn.
        assert [info.id for info in app.visible] == [ids["newest"], ids["older"]]
        assert app.list.text.startswith(app.title(app.visible[0]))
        assert "Add a theme" in app.list.text
        shown = app.detail.text()
        assert shown.startswith(f"{ids['newest'][:8]} · test:local · ")
        assert shown.endswith(
            "2 turns\n\n▌ Add a theme\n\n  Done\n\n▌ Now tests\n\n  Wrote tests for the theme"
        )
        # Words are AND-ed against prompts and the detail keeps only matching turns.
        app.query.text = "tests now"
        assert [info.id for info in app.visible] == [ids["newest"]]
        assert app.detail.text().endswith(
            "1 of 2 turns\n\n▌ Now tests\n\n  Wrote tests for the theme"
        )
        # Responses are searched only when asked.
        app.query.text = "patched"
        assert app.visible == []
        assert app.detail.text() == "No matching sessions."
        app.responses = True
        app.refresh()
        assert [info.id for info in app.visible] == [ids["older"]]
        # Widening to every workspace brings the other repo in.
        app.query.text = "cache"
        assert [info.id for info in app.visible] == [ids["older"]]
        app.everywhere = True
        app.refresh()
        assert [info.id for info in app.visible] == [ids["other"], ids["older"]]
        app.detail.set(app.details(app.visible[0]))
        assert app.detail.text().endswith("▌ Cache question elsewhere\n\n  (no response text)")


def test_browser_scopes_without_git(tmp_path):
    with (
        create_pipe_input() as pipe,
        patch("pcode.worktree.main_checkout", side_effect=FileNotFoundError),
    ):
        app, ids = browser(tmp_path, input=pipe)
        assert [info.id for info in app.visible] == [ids["newest"], ids["older"]]


def test_browser_marks_unreadable_and_empty_sessions(tmp_path):
    with create_pipe_input() as pipe:
        app, ids = browser(tmp_path, input=pipe)
        empty = SavedSession.create("test:local", tmp_path, app.root)
        empty.close()
        gone = empty.info.model_copy(update={"id": "missing"})
        app.detail.set(app.details(empty.info))
        assert app.detail.text().endswith("0 turns\n(No prompt yet)")
        app.detail.set(app.details(gone))
        assert app.detail.text() == "(Transcript unavailable)"


def test_browser_renders_responses_as_markdown(tmp_path):
    from pcode.runtime import Message

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.append("turn_started", prompt="Explain")
    saved.event(Message("## Heading\n\nSome *emphasis* here."))
    saved.append("turn_completed")
    saved.close()
    with create_pipe_input() as pipe:
        app = SessionBrowser(
            [saved.info], root=root, workspace=tmp_path, input=pipe, output=DummyOutput()
        )
        shown = app.detail.text(width=40)
        assert "##" not in shown and "*" not in shown
        assert "Heading" in shown and "emphasis" in shown
        # Styled fragments survive; emphasis is italic in every theme.
        assert any("italic" in style for style, *_ in app.detail.fragments(40))


def browsed_turn(tmp_path, events, prompt="Do the thing"):
    """Render one recorded turn's detail pane."""
    from pcode.runtime import ToolSummary

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.append("turn_started", prompt=prompt)
    for event in events:
        saved.event(event)
    saved.append("turn_completed")
    saved.close()
    with create_pipe_input() as pipe:
        app = SessionBrowser(
            [saved.info], root=root, workspace=tmp_path, input=pipe, output=DummyOutput()
        )
        return app.detail.text(width=100), ToolSummary


def test_tool_calls_appear_between_the_text_they_ran_between(tmp_path):
    from pcode.runtime import Message, ToolSummary

    shown, _ = browsed_turn(
        tmp_path,
        [
            Message("Looking now."),
            ToolSummary("read_file", "src/pcode/ui.py → 40 lines", elapsed_seconds=0.25),
            ToolSummary("shell", "ls → failed", failed=True, command="ls /nope\nsecond line"),
            Message("All done."),
        ],
    )
    body = shown.split("▌ Do the thing\n\n", 1)[1].splitlines()
    # Tool lines stay flush with each other and a blank row brackets the run,
    # the spacing scrollback gives the same sequence.
    assert body == [
        "  Looking now.",
        "",
        "  ✓ Read · src/pcode/ui.py → 40 lines · 0.2s",
        "  ✗ Run · ls → failed",
        "    ls /nope … [1 more lines]",
        "",
        "  All done.",
    ]


def test_long_turns_are_shown_whole(tmp_path):
    from pcode.runtime import Message

    prompt = "\n".join(f"prompt line {i}" for i in range(40))
    response = "\n".join(f"response line {i}" for i in range(40))
    shown, _ = browsed_turn(tmp_path, [Message(response)], prompt=prompt)
    assert "prompt line 39" in shown and "response line 39" in shown
    assert "…" not in shown


def test_scrollback_and_browser_share_one_tool_line(tmp_path):
    """Same glyph, label, and style; the browser only passes more of the detail."""
    from pcode.tool_display import tool_summary_lines

    (line,) = tool_summary_lines("read_file", " · src/x.py → 40 lines", elapsed_seconds=0.25)
    assert line.plain == "✓ Read · src/x.py → 40 lines · 0.2s"
    assert line.style == "pcode.thinking"
    failed, preview = tool_summary_lines("shell", "", failed=True, command="rm -rf /\nmore")
    assert failed.plain == "✗ Run"
    assert preview.plain == "  rm -rf / … [1 more lines]"
    # A width truncates rather than wraps, as the scrollback line does.
    (narrow,) = tool_summary_lines("read_file", " · " + "x" * 200, width=20)
    assert len(narrow.plain) == 20 and narrow.plain.endswith("…")


def test_markdown_links_do_not_leak_their_escape_wrapper():
    """Rich writes OSC 8 links; prompt_toolkit's ANSI parser would spill the wrapper."""
    from rich.markdown import Markdown

    from pcode.popup_ui import RichPane

    pane = RichPane()
    pane.set([Markdown("See [docs](https://example.com/page) for more.")])
    shown = pane.text(width=60)
    assert "See docs for more." in shown
    assert "8;id=" not in shown and "example.com" not in shown


def test_rich_pane_renders_current_content_in_one_pass():
    """Layout sizing must not leave the pane showing the previous frame's content."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.layout import Layout

    from pcode.popup_ui import RichPane

    pane = RichPane()
    pane.set([Text("first")])
    app = Application(layout=Layout(pane.window), output=DummyOutput())
    with set_app(app):
        control = pane.control
        assert control.preferred_width(80) is not None
        pane.set([Text("second")])
        assert control.preferred_width(80) is not None
        content = control.create_content(80, None)
        assert "second" in "".join(t for _, t in content.get_line(0))


def test_literal_keeps_every_line_and_drops_only_the_outer_blanks():
    assert literal("\n\none\n\ntwo\n\n") == "one\n\ntwo"
    # Long lines are not cut: the pane scrolls and wraps instead.
    assert literal("x" * 400) == "x" * 400
    # Terminal controls still cannot survive, and secrets are still redacted.
    assert "\x1b" not in literal("a\x1b[31mb")
    assert "hunter2" not in literal("export PASSWORD=hunter2")


@pytest.mark.parametrize("keys", ["\x1b", "\r", "q"])
def test_info_popup_closes_on_every_exit_key(keys):
    async def run():
        with create_pipe_input() as pipe:
            dialog = session_info_dialog(
                [("Model", "test:local"), ("Turns", "2")],
                input=pipe,
                output=DummyOutput(),
            )
            task = asyncio.create_task(dialog.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text(keys)
            assert await asyncio.wait_for(task, 2) is None

    asyncio.run(run())


def test_session_overview_reports_storage_and_feeds_context(tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    stream = StringIO()
    app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=stream, width=200))
    app.model = "test:local"
    app.runtime = AgentRuntime(Agent("test"), saved)
    try:
        rows = dict(app.session_overview())
        assert rows["Model"] == "test:local"
        assert rows["Session"] == saved.info.id
        assert rows["Saved in"] == str(saved.directory)
        # Nothing has been sent yet, so there is no resolved prompt to attribute.
        assert rows["Prompt overhead"] == "Measured on the first model request."
        # Without an editor, /status prints exactly what the popup shows.
        app.handle("/status")
        assert f"Saved in: {saved.directory}" in stream.getvalue()
    finally:
        app.runtime.close()


def test_session_overview_attributes_prompt_overhead_after_a_request(tmp_path):
    """The rows come from the last request's parameters, captured on the runtime."""
    from test_context_breakdown import isolated_workspace, request_parameters

    workspace = isolated_workspace()
    (workspace / "AGENTS.md").write_text("Repository rules.\n")
    stream = StringIO()
    app = PreviewApp(workspace=workspace, console=Console(file=stream, width=200))
    app.model = "test:local"
    app.runtime = AgentRuntime(Agent("test"), None)
    try:
        app.runtime.request_parameters = request_parameters(workspace)["parameters"]
        rows = dict(app.session_overview())
        assert "tokens" in rows["Prompt overhead"]
        assert rows["  Instructions"].startswith("~")
        assert "tools" in rows["  Tool schemas"]
        app.handle("/status")
        assert "AGENTS.md" in stream.getvalue()
    finally:
        app.runtime.close()


def test_status_requests_the_popup_only_with_a_live_editor(tmp_path):
    app = PreviewApp(workspace=tmp_path, console=Console(file=StringIO()))
    app.transcript.output = Mock()
    app.handle("/status")
    assert app.session_info_requested
    assert not app.session_requested
    with pytest.raises(ValueError, match="Usage: /status"):
        app.registry.find("/status").handler("extra")


def test_first_prompt_is_not_latest_and_handles_empty_session(tmp_path):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        assert first_prompt(saved.info, saved.directory.parent) == "(No prompt yet)"
        saved.append("turn_started", prompt="First question")
        saved.append("turn_started", prompt="Followup")
        assert first_prompt(saved.info, saved.directory.parent) == "First question"
    finally:
        saved.close()


def test_session_turns_pairs_prompts_with_final_responses(tmp_path):
    from pcode.runtime import Message

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    root = saved.directory.parent
    try:
        assert session_turns(saved.info, root) == []
        saved.append("turn_started", prompt="First question", run_id="a")
        saved.event(Message("Interim thoughts"))
        saved.event(Message("Final answer"))
        saved.append("turn_completed", run_id="a")
        saved.append("turn_started", prompt="Second", run_id="b")
        saved.append("turn_failed", run_id="b", error="boom")
        # A /resend retry keeps the turn and replaces its outcome.
        saved.append("turn_started", prompt="Second", run_id="c", continuation=True)
        saved.event(Message("Retry worked"))
        saved.append("turn_completed", run_id="c")
        saved.append("turn_started", prompt="Third", run_id="d")
        saved.append("turn_cancelled", run_id="d")
        with (saved.directory / "transcript.jsonl").open("a") as file:
            file.write('{"kind": "turn_started", "prompt": "torn')  # Torn final record.
        # `response` is the final text block; `blocks` keeps every one, in order.
        assert session_turns(saved.info, root) == [
            Turn(
                "First question", "Final answer", "complete", ["Interim thoughts", "Final answer"]
            ),
            Turn("Second", "Retry worked", "complete", ["Retry worked"]),
            Turn("Third", "", "cancelled", []),
        ]
        assert first_prompt(saved.info, root) == "First question"
    finally:
        saved.close()
    missing = saved.info.model_copy(update={"id": "nope"})
    assert session_turns(missing, root) is None
    assert first_prompt(missing, root) == "(Prompt unavailable)"


def test_resume_restores_before_replacing_runtime(tmp_path):
    async def run():
        root = tmp_path / "sessions"
        saved = SavedSession.create("test:local", tmp_path, root)
        identity = saved.info.id

        async def model(messages, info):
            yield "Saved answer"

        agent = Agent(FunctionModel(stream_function=model))
        previous = AgentRuntime(agent, saved)
        _ = [event async for event in previous.stream("First question")]
        history = previous.history
        previous.close()
        app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
        app.handle("/resume")
        assert app.session_requested
        with patch("pcode.agent.create_agent", return_value=agent):
            await app.resume_session(identity)
        try:
            assert app.runtime.session.info.id == identity
            assert app.runtime.conversation_id == identity
            assert app.model == "test:local"
            assert app.runtime.history == history
            assert app.runtime.turns == 1
            original = app.runtime
            await app.resume_session(identity)
            assert app.runtime is original
            (tmp_path / "other").mkdir()
            other = SavedSession.create("test:local", tmp_path / "other", root)
            other_id = other.info.id
            other.close()
            with pytest.raises(SessionError, match="cross-repo"):
                await app.resume_session(other_id)
            assert app.runtime is original
            # The failed candidate's lock was released.
            reopened = SavedSession.open(other_id, root)
            reopened.close()
        finally:
            app.runtime.close()

    asyncio.run(run())


def test_failed_recovery_keeps_current_conversation(tmp_path):
    async def run():
        root = tmp_path / "sessions"
        saved = SavedSession.create("test:local", tmp_path, root)
        identity = saved.info.id
        saved.close()
        app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
        original = app.runtime
        with (
            patch("pcode.agent.create_agent", return_value=Agent("test")),
            patch("pcode.live.AgentRuntime.restore", side_effect=SessionError("broken")),
            pytest.raises(SessionError, match="broken"),
        ):
            await app.resume_session(identity)
        assert app.runtime is original
        reopened = SavedSession.open(identity, root)
        reopened.close()

    asyncio.run(run())
