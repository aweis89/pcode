import asyncio
from io import StringIO
from unittest.mock import AsyncMock, Mock, patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from rich.console import Console

from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.model_ui import ModelPicker
from pcode.sessions import SavedSession
from pcode.ui import create_prompt

MODELS = ["anthropic:claude-opus-5", "openai-codex:gpt-5.6-luna"]
PROVIDERS = {"anthropic", "openai-codex"}


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "keys,expected",
    [
        ("\r", MODELS[0]),
        ("\x1b[B\r", MODELS[1]),
        ("luna\r", MODELS[1]),
        ("anthopus\r", MODELS[0]),
        ("codluna\r", MODELS[1]),
        ("anthropic:new-model\r", "anthropic:new-model"),
        ("\x1b", None),
        ("\x03", None),
        ("\x0c", None),
    ],
)
def test_picker_keyboard(keys, expected):
    async def run():
        with create_pipe_input() as pipe:
            picker = ModelPicker(
                MODELS, PROVIDERS, current=MODELS[0], input=pipe, output=DummyOutput()
            )
            task = asyncio.create_task(picker.run())
            try:
                await wait_for(lambda: picker.app.is_running)
                pipe.send_text(keys)
                assert await asyncio.wait_for(task, 3) == expected
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_filter_empty_matches_custom_validation_and_current_marker():
    with create_pipe_input() as pipe:
        picker = ModelPicker(MODELS, PROVIDERS, current=MODELS[0], input=pipe, output=DummyOutput())
        assert "current" in str(picker.fragments())
        picker.search.text = "no matches"
        assert picker.matches == []
        picker.search.text = "google:not-active"
        assert picker.matches == []
        picker.search.text = "anthropic:invalid model"
        assert picker.matches == []
        picker.search.text = "opus 5"
        assert picker.matches == [MODELS[0]]
        picker.search.text = "anthropic:custom-id"
        assert picker.matches == ["anthropic:custom-id"]
        assert "custom" in str(picker.fragments())


def test_ctrl_l_preserves_editor_draft_and_cursor():
    async def run():
        app = PreviewApp(console=Console(file=StringIO()))
        on_model = Mock()
        with create_pipe_input() as pipe:
            session = create_prompt(
                app.registry, on_model=on_model, input=pipe, output=DummyOutput()
            )
            task = asyncio.create_task(session.prompt_async())
            try:
                await wait_for(lambda: session.app.is_running)
                pipe.send_text("draft\x1b[D\x0c")
                await wait_for(lambda: on_model.called)
                assert session.default_buffer.text == "draft"
                assert session.default_buffer.cursor_position == 4
                assert not task.done()
                pipe.send_text("\r")
                assert await asyncio.wait_for(task, 3) == "draft"
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("save", [False, True])
def test_switch_from_preview_is_lazy_and_honors_save(monkeypatch, tmp_path, save):
    monkeypatch.setattr("pcode.agent.create_agent", lambda *args: Agent("test"))
    root = tmp_path / "sessions"
    app = PreviewApp(
        workspace=tmp_path, session_dir=root, save=save, console=Console(file=StringIO())
    )

    async def run():
        await app.switch_model(MODELS[0])
        assert app.model == MODELS[0]
        assert app.runtime.session is None
        assert not root.exists()
        assert app.runtime.history == []
        assert (app.runtime.session_factory is not None) == save
        _ = [event async for event in app.runtime.stream("hello")]
        if save:
            assert app.runtime.session.info.model == MODELS[0]
            assert app.runtime.session.info.workspace == str(tmp_path)
        else:
            assert not root.exists()
        app.runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("save", [False, True])
def test_switch_continues_conversation(monkeypatch, tmp_path, save):
    from pydantic_ai.models.function import FunctionModel

    requests = []

    async def respond(messages, info):
        requests.append(list(messages))
        yield "remembered answer"

    monkeypatch.setattr(
        "pcode.agent.create_agent", lambda *args: Agent(FunctionModel(stream_function=respond))
    )
    root = tmp_path / "sessions"
    saved = SavedSession.create(MODELS[0], tmp_path, root) if save else None
    runtime = AgentRuntime(Agent("test"), saved)
    app = PreviewApp(
        model=MODELS[0],
        workspace=tmp_path,
        saved_session=saved,
        runtime=runtime,
        console=Console(file=StringIO()),
    )

    async def run():
        _ = [event async for event in runtime.stream("old question")]
        old_id = runtime.conversation_id
        history = list(runtime.history)
        plan_store = runtime.plan_store
        inspections = runtime.inspections
        usage = (runtime.input_tokens, runtime.output_tokens)
        app.activity.plan = [{"content": "old plan"}]
        runtime.agent.model_settings = {"temperature": 0.5}
        await app.switch_model(MODELS[1])
        assert app.runtime is runtime
        assert app.model == MODELS[1]
        assert runtime.history == history
        assert runtime.conversation_id == old_id
        assert runtime.turns == 1
        assert (runtime.input_tokens, runtime.output_tokens) == usage
        assert runtime.plan_store is plan_store
        assert runtime.inspections is inspections
        assert runtime.agent.model_settings is None
        assert app.activity.plan == [{"content": "old plan"}]
        assert runtime.session is saved
        if save:
            from pcode.sessions import read_info

            assert read_info(saved.directory).model == MODELS[1]
        _ = [event async for event in runtime.stream("follow-up question")]
        assert requests[0][: len(history)] == history
        assert runtime.turns == 2
        assert runtime.conversation_id == old_id
        if save:
            assert runtime.session.info.id == old_id
            assert len(list(root.iterdir())) == 1
            recovered = await runtime.session.recover()
            assert recovered == runtime.history
        else:
            assert runtime.session is None
            assert not root.exists()
        runtime.reset()
        _ = [event async for event in runtime.stream("new question")]
        assert runtime.conversation_id != old_id
        if save:
            assert runtime.session.info.model == MODELS[1]
        runtime.close()

    asyncio.run(run())


def test_switch_manifest_failure_keeps_conversation(monkeypatch, tmp_path):
    saved = SavedSession.create(MODELS[0], tmp_path, tmp_path / "sessions")
    runtime = AgentRuntime(Agent("test"), saved)
    app = PreviewApp(
        model=MODELS[0],
        runtime=runtime,
        saved_session=saved,
        console=Console(file=StringIO()),
    )
    old_agent = runtime.agent
    monkeypatch.setattr("pcode.agent.create_agent", lambda *args: Agent("test"))
    monkeypatch.setattr(saved, "save_info", Mock(side_effect=OSError("disk full")))
    try:
        with pytest.raises(OSError, match="disk full"):
            asyncio.run(app.switch_model(MODELS[1]))
        assert app.runtime is runtime
        assert runtime.agent is old_agent
        assert app.model == saved.info.model == MODELS[0]
    finally:
        runtime.close()


def test_replacing_agent_rebinds_planning_store():
    from pydantic_ai_harness import Planning

    runtime = AgentRuntime(Agent("test"))
    planning = Planning()
    runtime.replace_agent(Agent("test", capabilities=[planning]))
    assert planning.store_resolver(None) is runtime.plan_store
    runtime.reset()
    assert planning.store_resolver(None) is runtime.plan_store


def test_failed_switch_and_same_model_keep_runtime(monkeypatch, tmp_path):
    runtime = AgentRuntime(Agent("test"))
    app = PreviewApp(model=MODELS[0], runtime=runtime, console=Console(file=StringIO()))
    create = Mock(side_effect=ValueError("bad provider"))
    monkeypatch.setattr("pcode.agent.create_agent", create)

    async def run():
        await app.switch_model(MODELS[0])
        create.assert_not_called()
        with pytest.raises(ValueError, match="bad provider"):
            await app.switch_model(MODELS[1])
        assert app.runtime is runtime
        assert app.model == MODELS[0]
        app.activity.busy = True
        with pytest.raises(ValueError, match="Cannot change models"):
            await app.switch_model(MODELS[1])
        assert app.runtime is runtime

    asyncio.run(run())


@pytest.mark.parametrize("command", ["/model\r", "\x0c"])
@pytest.mark.parametrize("busy", [False, True])
def test_picker_shortcut_uses_serialized_command_flow(command, busy):
    async def run():
        output = StringIO()
        app = PreviewApp(console=Console(file=output))
        app.activity.busy = busy
        choose = AsyncMock()
        app.choose_model = choose
        session = None
        with create_pipe_input() as pipe:

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait_for(lambda: session is not None and session.app.is_running)
                    pipe.send_text(command)
                    if busy:
                        await wait_for(lambda: "unavailable while working" in output.getvalue())
                        choose.assert_not_called()
                    else:
                        await wait_for(lambda: choose.called)
                        choose.assert_awaited_once()
                    pipe.send_text("/quit\r")
                    await asyncio.wait_for(task, 3)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("selection", [None, MODELS[1]])
def test_modal_result_is_applied_only_on_accept(monkeypatch, selection):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    app = PreviewApp(model=MODELS[0], runtime=Mock(), console=Console(file=StringIO()))
    app.model_requested = True
    switch = AsyncMock()
    app.switch_model = switch
    monkeypatch.setattr("pcode.models.active_providers", lambda _: PROVIDERS)
    picker = Mock(run=AsyncMock(return_value=selection))
    factory = Mock(return_value=picker)
    monkeypatch.setattr("pcode.model_ui.ModelPicker", factory)

    @asynccontextmanager
    async def terminal():
        yield

    monkeypatch.setattr("pcode.app.in_terminal", terminal)

    async def run():
        output = SimpleNamespace(lock=asyncio.Lock(), flush=AsyncMock())
        session = SimpleNamespace(app=SimpleNamespace(input=object(), output=object(), style=None))
        await app.choose_model(output, session)
        assert not app.model_requested
        if selection:
            switch.assert_awaited_once_with(selection)
        else:
            switch.assert_not_called()
        assert factory.call_args.kwargs["current"] == MODELS[0]

    asyncio.run(run())


def test_no_active_provider_does_not_open_modal(monkeypatch):
    buffer = StringIO()
    app = PreviewApp(console=Console(file=buffer))
    monkeypatch.setattr("pcode.models.active_providers", lambda _: set())
    factory = Mock(side_effect=AssertionError("must not open"))
    monkeypatch.setattr("pcode.model_ui.ModelPicker", factory)
    app.model_requested = True
    asyncio.run(app.choose_model(None, None))
    assert not app.model_requested
    assert "No active model providers" in buffer.getvalue()
    factory.assert_not_called()


@pytest.mark.parametrize(
    "query,expected",
    [
        ("anthopus", ["anthropic:claude-opus-5"]),
        ("ANTHOPUS", ["anthropic:claude-opus-5"]),
        ("anth opus", ["anthropic:claude-opus-5"]),
        ("opus anth", ["anthropic:claude-opus-5"]),
        ("claopus", ["anthropic:claude-opus-5"]),
        ("codluna", ["openai-codex:gpt-5.6-luna"]),
        ("openai luna", ["openai-codex:gpt-5.6-luna"]),
        ("sonnet", ["anthropic:claude-sonnet-5"]),
        ("gpt-5.6", ["openai-codex:gpt-5.6-luna"]),
        ("opus luna", []),
    ],
)
def test_filter_combines_provider_and_model_prefixes(query, expected):
    models = [*MODELS, "anthropic:claude-sonnet-5", "anthropic:claude-haiku-4-5"]
    with create_pipe_input() as pipe:
        picker = ModelPicker(models, PROVIDERS, input=pipe, output=DummyOutput())
        picker.selected = 2
        picker.search.text = query
        assert picker.matches == expected
        assert picker.selected == 0
        picker.search.text = ""
        assert picker.matches == models


def test_filter_preserves_newest_first_catalog_order():
    from pcode.models import model_catalog

    current = "anthropic:claude-opus-4-5"
    with create_pipe_input() as pipe:
        picker = ModelPicker(
            model_catalog({"anthropic"}, current),
            {"anthropic"},
            current=current,
            input=pipe,
            output=DummyOutput(),
        )
        picker.search.text = "anthopus"
        assert picker.matches[0] == "anthropic:claude-opus-5"
        assert picker.matches.index("anthropic:claude-opus-4-8") < picker.matches.index(current)
        assert "current" in str(picker.fragments())
