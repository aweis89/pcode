"""Side questions run beside a turn without joining, steering, or blocking it."""

import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.planning import Planning
from pydantic_ai_harness.shell import Shell
from pydantic_ai_harness.subagents import SubAgents
from rich.console import Console

from pcode.agent import create_aside_agent, create_coder
from pcode.app import PreviewApp
from pcode.aside import Aside, Asides, settled_context
from pcode.live import AgentRuntime
from pcode.mcp_notice import MCPServers
from pcode.preferences import save_preferences
from pcode.runtime import Message
from pcode.ui import create_prompt


def test_settled_context_stops_before_an_unanswered_tool_call():
    call = ToolCallPart("read_file", {}, tool_call_id="one")
    messages = [
        ModelRequest([UserPromptPart("first")]),
        ModelResponse([call]),
        ModelRequest([ToolReturnPart("read_file", "ok", tool_call_id="one")]),
        ModelResponse([TextPart("answer")]),
        ModelRequest([UserPromptPart("second")]),
        ModelResponse([ToolCallPart("read_file", {}, tool_call_id="two")]),
    ]
    # The request in flight ends mid-tool-loop; a provider would reject it.
    assert settled_context(messages) == messages[:5]
    assert settled_context(messages[:2]) == messages[:1]
    assert settled_context([]) == []


def test_aside_agent_shares_the_model_but_keeps_nothing_that_can_change_the_workspace(tmp_path):
    coder = create_coder(tmp_path)
    main = Agent(TestModel(), capabilities=[coder], model_settings={"temperature": 0.5})
    aside = create_aside_agent(main, tmp_path)
    assert aside.model is main.model
    # Effort and thinking change the live agent's settings; a copy would go stale.
    assert not aside.model_settings
    capabilities = aside.root_capability.capabilities
    assert not [c for c in capabilities if isinstance(c, (Shell, SubAgents, Planning, MCPServers))]
    filesystem = next(c for c in capabilities if isinstance(c, FileSystem))
    assert filesystem.read_only
    # Its own instances: two concurrent runs may not share one filesystem or shell.
    assert filesystem is not next(c for c in coder.capabilities if isinstance(c, FileSystem))
    assert [c for c in coder.capabilities if isinstance(c, (Shell, SubAgents, Planning))]
    assert [c for c in coder.capabilities if isinstance(c, MCPServers)]


def test_aside_answers_while_a_turn_runs_and_records_nothing(tmp_path):
    (tmp_path / "sample.txt").write_text("workspace marker")
    blocked = asyncio.Event()
    released = asyncio.Event()
    side_tools: set[str] = set()
    side_requests = []

    async def model(messages, info):
        names = {tool.name for tool in info.function_tools}
        if "shell" not in names:  # The read-only side agent, not the conversation's.
            side_tools.update(names)
            prompts = [
                part.content
                for message in messages
                for part in message.parts
                if isinstance(part, UserPromptPart)
            ]
            side_requests.append(sum(isinstance(m, ModelRequest) for m in messages))
            # It sees the turn in flight, and its question comes last.
            assert prompts[0] == "Main task"
            yield f"Answer: {prompts[-1]}"
            return
        if not blocked.is_set():
            blocked.set()
            await released.wait()
            yield {0: DeltaToolCall(name="read_file", json_args='{"path":"sample.txt"}')}
        else:
            yield "Main answer."

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        async def turn():
            return [event async for event in runtime.stream("Main task")]

        main = asyncio.create_task(turn())
        await blocked.wait()
        reports = []
        answer = await runtime.aside(
            "Why this file?", report=lambda text, activity: reports.append((text, activity))
        )
        released.set()
        events = await main
        assert answer == "Answer: Why this file?"
        assert reports[-1] == ("Answer: Why this file?", "")
        assert Message("Main answer.") in events
        # Mid-turn the question joins the request in flight rather than following
        # it as a second user message, which providers reject.
        assert side_requests == [1]
        # Idle, the conversation ends on an answer, so it is asked normally.
        assert await runtime.aside("And now?") == "Answer: And now?"
        assert side_requests == [1, 3]
        # No node, no journal record, and the question never reaches history.
        assert len(runtime.tree.nodes) == 1
        prompts = [
            part.content
            for message in runtime.history
            for part in message.parts
            if isinstance(part, UserPromptPart)
        ]
        assert prompts == ["Main task"]
        assert "read_file" in side_tools
        assert not {"shell", "write_file", "edit_file", "delegate_task"} & side_tools
        # The tokens were really spent, so they are billed to the session.
        assert runtime.input_tokens > 0

    asyncio.run(run())


def test_asides_track_running_unread_failed_and_cancelled():
    async def run():
        asides = Asides()
        settled = []
        asides.on_settle = settled.append
        release = asyncio.Event()

        async def answer(aside):
            asides.update(aside, answer="partial", activity="Reading read_file")
            await release.wait()
            asides.update(aside, answer="done", activity="")

        async def fail(aside):
            raise ValueError("provider refused")

        first = asides.start("why", answer)
        await asyncio.sleep(0)
        assert asides.running == 1
        assert asides.unread == 0
        assert first.answer == "partial"
        assert asides.latest() is first
        asides.start("later", fail)
        await asyncio.sleep(0.05)
        assert [aside.status for aside in asides.items] == ["running", "failed"]
        assert "Run failed (ValueError)" in asides.items[1].error
        assert asides.unread == 1
        release.set()
        await asyncio.sleep(0.05)
        assert first.status == "answered"
        assert first.answer == "done"
        assert first.finished is not None
        assert asides.unread == 2
        assert asides.running == 0
        assert [aside.question for aside in settled] == ["later", "why"]
        # Reading the newest unread is what clears the footer count.
        assert asides.latest() is asides.items[1]
        asides.items[1].read = True
        assert asides.latest() is first

        forever = asyncio.Event()
        stopped = asides.start("stopped", lambda aside: forever.wait())
        await asyncio.sleep(0)
        assert asides.cancel() == 1
        await asides.close()
        assert stopped.status == "cancelled"

    asyncio.run(run())


def test_running_side_questions_get_muted_spinner_rows_above_the_editor():
    from pcode.aside import Aside
    from pcode.ui import ASIDE_ROWS, Activity

    activity = Activity()
    assert activity.aside_rows("⠋", 80) == []
    assert not activity.asides_running
    asides = Asides()
    activity.asides = asides.items
    running = Aside(question="why recursive descent?", activity="Reading parser.py")
    asides.items.append(running)
    asides.items.append(Aside(question="old", status="answered", activity=""))
    assert activity.asides_running
    rows = activity.aside_rows("⠋", 80)
    assert len(rows) == 1
    style, text = rows[0]
    assert style == "class:activity.aside"
    assert text.startswith("⠋ btw · why recursive descent? · Reading parser.py · ")
    assert text.endswith("s")
    # Narrow panes truncate rather than wrap so the editor keeps its rows.
    assert len(activity.aside_rows("⠋", 20)[0][1]) <= 20
    for index in range(ASIDE_ROWS + 1):
        asides.items.append(Aside(question=f"q{index}"))
    rows = activity.aside_rows("⠋", 80)
    assert len(rows) == ASIDE_ROWS
    assert rows[-1][1] == "… 3 more (/btw)"
    running.settle("answered")
    assert [text for _, text in activity.aside_rows("⠋", 80)][0].startswith("⠋ btw · q0")


def test_btw_command_requires_a_question_or_an_answer_to_read():
    preview = PreviewApp(console=Console(file=StringIO()))
    with pytest.raises(ValueError, match="No side questions yet"):
        preview.registry.dispatch("/btw")
    with pytest.raises(ValueError, match="local UI preview"):
        preview.registry.dispatch("/btw why")

    app = PreviewApp(
        model="test:local",
        runtime=AgentRuntime(Agent(TestModel())),
        console=Console(file=StringIO()),
    )
    # Unavailable commands are refused while working; this one is the exception.
    app.activity.busy = True
    app.activity.queued = 1
    assert app.registry.dispatch("/btw  why this file?  ")
    assert app.aside_requested == "why this file?"
    app.asides.items.append(Aside(question="earlier"))
    assert app.registry.dispatch("/btw")
    assert app.aside_view_requested


def test_btw_runs_in_the_background_and_reports_in_the_footer_and_transcript():
    async def run():
        output = StringIO()
        answered = asyncio.Event()

        class Runtime:
            session = None
            tree = None

            async def aside(self, question, report):
                report("", "Waiting for model…")
                await answered.wait()
                report(f"Answer to {question}", "")
                return f"Answer to {question}"

        app = PreviewApp(
            model="test:local",
            runtime=Runtime(),
            console=Console(file=output, color_system=None, width=140),
        )
        app.start_aside("why")
        await asyncio.sleep(0)
        assert app.asides.running == 1
        # A side question is not "working": input and the queue stay untouched.
        assert not app.activity.busy
        assert app.asides.items[0].activity == "Waiting for model…"
        answered.set()
        await asyncio.sleep(0.05)
        assert app.asides.items[0].answer == "Answer to why"
        assert app.asides.unread == 1
        await app.asides.close()
        text = " ".join(output.getvalue().split())
        assert "Asking beside the conversation, read-only" in text

    asyncio.run(run())


def test_btw_asks_and_reads_while_a_turn_is_running():
    """The real command path: neither asking nor reading waits for the turn."""

    async def run():
        printed = StringIO()
        streaming = asyncio.Event()
        finish = asyncio.Event()
        answered = asyncio.Event()

        class Runtime:
            session = None
            tree = None

            async def stream(self, prompt):
                streaming.set()
                await finish.wait()
                yield Message("main answer")

            async def aside(self, question, report):
                report("", "Waiting for model…")
                await answered.wait()
                report("side answer", "")
                return "side answer"

        app = PreviewApp(
            model="test:local",
            runtime=Runtime(),
            console=Console(file=printed, color_system=None, width=140),
        )
        browsers = []

        class Browser:
            def __init__(self, asides, **options):
                self.asides = asides
                self.running = False
                browsers.append(self)

            async def run(self):
                self.running = True
                await asyncio.sleep(0.05)
                self.running = False

        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            async def wait_for(predicate):
                async with asyncio.timeout(5):
                    while not predicate():
                        await asyncio.sleep(0.01)

            async def drive():
                await wait_for(lambda: session is not None and session.app.is_running)
                pipe.send_text("do the work\r")
                await asyncio.wait_for(streaming.wait(), 5)
                pipe.send_text("/btw why this file?\r")
                await wait_for(lambda: app.asides.running == 1)
                # The turn is untouched: nothing queued, nothing cancelled.
                assert app.activity.queued_prompts == []
                assert "btw running" in "".join(
                    value for _, value in app.toolbar() if isinstance(value, str)
                )
                answered.set()
                await wait_for(lambda: app.asides.unread == 1)
                await wait_for(lambda: "Side answer ready" in printed.getvalue())
                pipe.send_text("/btw\r")
                await wait_for(lambda: bool(browsers))
                assert browsers[0].asides is app.asides
                await wait_for(lambda: not browsers[0].running)
                finish.set()
                await wait_for(lambda: "main answer" in printed.getvalue())
                pipe.send_text("/quit\r")

            with (
                patch("pcode.app.create_prompt", prompt),
                patch("pcode.aside_ui.AsideBrowser", Browser),
            ):
                await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=15)
        assert [aside.answer for aside in app.asides.items] == ["side answer"]

    asyncio.run(run())


def test_a_ready_side_answer_opens_the_viewer_unless_auto_open_is_off():
    """The answer arrives where the reader is looking, without a second /btw."""

    async def run():
        printed = StringIO()
        answered = asyncio.Event()

        class Runtime:
            session = None
            tree = None

            async def aside(self, question, report):
                await answered.wait()
                report(f"answer to {question}", "")
                return f"answer to {question}"

        app = PreviewApp(
            model="test:local",
            runtime=Runtime(),
            console=Console(file=printed, color_system=None, width=140),
        )
        browsers = []

        class Browser:
            def __init__(self, asides, **options):
                browsers.append(self)

            async def run(self):
                await asyncio.sleep(0)

        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            async def wait_for(predicate):
                async with asyncio.timeout(5):
                    while not predicate():
                        await asyncio.sleep(0.01)

            async def drive():
                await wait_for(lambda: session is not None and session.app.is_running)
                pipe.send_text("/btw why this file?\r")
                await wait_for(lambda: app.asides.running == 1)
                answered.set()
                # Nobody typed a bare /btw: the settled answer opened the viewer.
                await wait_for(lambda: len(browsers) == 1)
                await wait_for(lambda: "Opening it." in printed.getvalue())
                save_preferences(btw_auto_open="off")
                answered.clear()
                pipe.send_text("/btw and this one?\r")
                await wait_for(lambda: app.asides.running == 1)
                answered.set()
                await wait_for(lambda: "/btw opens it." in printed.getvalue())
                await asyncio.sleep(0.05)
                assert len(browsers) == 1
                pipe.send_text("/quit\r")

            with (
                patch("pcode.app.create_prompt", prompt),
                patch("pcode.aside_ui.AsideBrowser", Browser),
            ):
                await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=15)

    asyncio.run(run())


def test_aside_browser_streams_the_answer_and_marks_it_read():
    from pcode.aside_ui import AsideBrowser

    asides = Asides()
    running = Aside(question="why this file?", answer="Because", activity="Reading read_file")
    asides.items.append(running)
    browser = AsideBrowser(asides, output=None, input=None)
    assert "why this file?" in browser.list.text
    assert "(running" in browser.list.text
    body = browser.detail.text(80)
    assert "why this file?" in body
    assert "Because" in body
    assert "Reading read_file" in body
    # A running answer is not read yet, so the footer keeps announcing it.
    assert not running.read
    asides.update(running, answer="Because the task named it.", activity="")
    running.settle("answered")
    browser.refresh()
    assert "Because the task named it." in browser.detail.text(80)
    assert running.read
    assert "(answered" in browser.list.text


def test_aside_browser_copies_the_selected_answer(monkeypatch):
    from pcode import aside_ui
    from pcode.aside_ui import AsideBrowser

    copies: list[str] = []

    def fake_copy(text, output=None):
        copies.append(text)
        return True, False

    monkeypatch.setattr(aside_ui, "copy_to_clipboard", fake_copy)
    asides = Asides()
    browser = AsideBrowser(asides, output=None, input=None)
    browser.copy()
    assert browser.notice == "No answer to copy"
    assert copies == []

    aside = Aside(question="why?", answer="Because **it** works.")
    aside.settle("answered")
    asides.items.append(aside)
    browser.refresh()
    browser.copy()
    assert copies == ["Because **it** works."]
    assert browser.notice == "Copied answer"

    monkeypatch.setattr(aside_ui, "copy_to_clipboard", lambda text, output=None: (False, False))
    browser.copy()
    assert browser.notice == "Could not copy answer"


def test_tree_browser_is_read_only_while_a_turn_runs():
    from pcode.conversation_tree import ConversationTree
    from pcode.tree_ui import TreeBrowser

    tree = ConversationTree()
    tree.consume({"kind": "turn_started", "run_id": "a", "parent_id": None, "prompt": "question"})
    tree.consume({"kind": "Message", "markdown": "answer"})
    browser = TreeBrowser(tree, navigable=False, output=None, input=None)
    labels = " ".join(
        fragment_list_to_text(to_formatted_text(window.content.text))
        for window in browser.app.layout.find_all_windows()
        if hasattr(window.content, "text")
    )
    assert "read-only while working" in labels
    assert "Forking waits for the running turn" in labels
    exits = []
    browser.app.exit = lambda **kwargs: exits.append(kwargs)
    enter = next(
        binding for binding in browser.app.key_bindings.bindings if binding.keys == (Keys.ControlM,)
    )
    event = SimpleNamespace(app=browser.app)
    enter.handler(event)
    assert exits == []
    browser.navigable = True
    enter.handler(event)
    assert exits == [{"result": ("a", False)}]
