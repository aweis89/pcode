"""`/btw $MODEL ... QUESTION` asks side questions on chosen models."""

import asyncio
from io import StringIO

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from rich.console import Console

from pcode.agent import SideModel, create_coder, side_model
from pcode.app import PreviewApp
from pcode.aside import (
    ASIDE_FRAMING,
    ASIDE_MODEL_LIMIT,
    Aside,
    Asides,
    model_fragment,
    model_labels,
    parse_models,
)
from pcode.commands import SlashCompleter
from pcode.live import AgentRuntime
from pcode.preferences import save_model_effort


def test_leading_dollar_words_select_models_and_the_rest_is_the_question():
    assert parse_models("why?") == ([], "why?")
    assert parse_models("  $a:x   is  this right? ") == (["a:x"], "is  this right?")
    assert parse_models("$a:x $b:y $a:x second opinions?") == (["a:x", "b:y"], "second opinions?")
    # Only leading words name models; a `$` later on is part of the question.
    assert parse_models("$a:x costs $5?") == (["a:x"], "costs $5?")
    assert parse_models("what does $HOME hold?") == ([], "what does $HOME hold?")
    with pytest.raises(ValueError, match="No question after the model"):
        parse_models("$a:x")
    with pytest.raises(ValueError, match="No question after the model"):
        parse_models("$a:x $b:y  ")
    with pytest.raises(ValueError, match="model name must follow"):
        parse_models("$ why?")
    models = " ".join(f"$p:m{index}" for index in range(ASIDE_MODEL_LIMIT))
    assert len(parse_models(f"{models} q")[0]) == ASIDE_MODEL_LIMIT
    # Duplicates collapse before the cap is counted.
    assert len(parse_models(f"{models} $p:m0 q")[0]) == ASIDE_MODEL_LIMIT
    with pytest.raises(ValueError, match=f"At most {ASIDE_MODEL_LIMIT} models"):
        parse_models(f"{models} $p:extra q")


def test_labels_drop_the_provider_unless_two_models_would_look_the_same():
    assert model_labels(["anthropic:claude-x", "openai-codex:gpt-y"]) == {
        "anthropic:claude-x": "claude-x",
        "openai-codex:gpt-y": "gpt-y",
    }
    assert model_labels(["anthropic:claude-x", "meridian:claude-x", "test"]) == {
        "anthropic:claude-x": "anthropic:claude-x",
        "meridian:claude-x": "meridian:claude-x",
        "test": "test",
    }


def test_dollar_completes_models_only_among_leading_btw_words(monkeypatch):
    app = PreviewApp(console=Console(file=StringIO()))
    monkeypatch.setattr(
        app, "model_suggestions", lambda: ["anthropic:claude-opus", "openai-codex:gpt-6"]
    )
    completer = SlashCompleter(app.registry)

    def complete(text):
        return [
            (item.text, item.start_position)
            for item in completer.get_completions(Document(text), CompleteEvent())
        ]

    assert complete("/btw $") == [("$anthropic:claude-opus", -1), ("$openai-codex:gpt-6", -1)]
    assert complete("/btw $opus") == [("$anthropic:claude-opus", -5)]
    # Later model words complete too.
    assert complete("/btw $anthropic:claude-opus $gpt") == [("$openai-codex:gpt-6", -4)]
    # Once the question starts, `$` is text.
    assert complete("/btw why $") == []
    assert complete("/btw $opus ") == []
    assert complete("/btw why") == []
    # Nowhere else: not a normal prompt, shell mode, or another command.
    assert complete("$opus") == []
    assert complete("!echo $opus") == []
    assert complete("/compact $opus") == []
    assert model_fragment("$a $b") == "b"
    assert model_fragment("") is None


def test_model_suggestions_come_from_the_model_picker_catalog(monkeypatch):
    from pcode import models

    app = PreviewApp(model="test:local", console=Console(file=StringIO()))
    calls = []

    def providers(current):
        calls.append(current)
        return {"meridian"}

    monkeypatch.setattr(models, "active_providers", providers)
    first = app.model_suggestions()
    assert first == models.model_catalog({"meridian"}, "test:local")
    assert first and all(name.startswith("meridian:") for name in first)
    # Typing does not rescan providers on every keystroke.
    assert app.model_suggestions() is first
    assert calls == ["test:local"]


def test_side_model_uses_the_chosen_models_own_settings(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    save_model_effort("openai:gpt-5", "high")
    chosen = side_model("openai:gpt-5")
    assert chosen.name == "openai:gpt-5"
    assert chosen.model.model_name == "gpt-5"
    assert chosen.settings == {"openai_reasoning_effort": "high"}
    # Without a saved effort, only the model's defaults.
    assert side_model("openai:gpt-5.1").settings is None
    with pytest.raises(ValueError, match="Cannot use nowhere:model"):
        side_model("nowhere:model")
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ValueError, match="Cannot use openai:gpt-5"):
        side_model("openai:gpt-5")


class Recorder(AbstractCapability):
    """Records each request's conversation id and settings."""

    def __init__(self, seen):
        self.seen = seen

    async def before_model_request(self, ctx, request_context):
        aside = any(
            isinstance(part, UserPromptPart) and str(part.content).startswith(ASIDE_FRAMING)
            for message in request_context.messages
            for part in message.parts
        )
        self.seen.append(
            (
                aside,
                request_context.model.model_name,
                ctx.conversation_id,
                request_context.model_settings,
            )
        )
        return request_context


def test_another_model_runs_with_its_settings_and_its_own_conversation_id(tmp_path):
    seen = []

    async def main_model(messages, info):
        yield "main"

    async def other_model(messages, info):
        yield "other"

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=main_model, model_name="main"),
            capabilities=[create_coder(tmp_path), Recorder(seen)],
            model_settings={"temperature": 0.5, "anthropic_effort": "high"},
        )
    )
    other = SideModel(
        "fn:other", FunctionModel(stream_function=other_model, model_name="other"), {"top_p": 0.9}
    )

    async def run():
        [event async for event in runtime.stream("Main task")]
        assert await runtime.aside("same?") == "main"
        assert await runtime.aside("other?", model=other) == "other"
        assert await runtime.aside("again?", model=other) == "other"
        # The override ended with the run: the conversation keeps its settings.
        [event async for event in runtime.stream("Next")]

    asyncio.run(run())
    main, same, first, second, following = seen
    conversation = runtime.conversation_id
    assert main[:3] == (False, "main", conversation)
    # The default path is the conversation's own request, cache identity included.
    assert same[:3] == (True, "main", conversation)
    assert same[3] == main[3]
    for request in (first, second):
        aside, name, identity, settings = request
        assert (aside, name) == (True, "other")
        assert identity.startswith(f"{conversation}.btw-")
        # Nothing of the conversation model's settings leaks into another model.
        assert settings.get("top_p") == 0.9
        assert "temperature" not in settings
        assert "anthropic_effort" not in settings
    assert first[2] != second[2]
    assert following[2] == conversation
    assert following[3] == main[3]


class FanOutRuntime:
    session = None
    tree = None

    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()

    async def aside(self, question, report, model=None):
        self.calls.append(model.name if model else None)
        await self.release.wait()
        if model is not None and model.name == "p:broken":
            raise ValueError("provider refused")
        report(f"{model.name if model else 'own'} says hi", "")
        return "done"


def fan_out_app(monkeypatch, runtime):
    from pcode import agent

    monkeypatch.setattr(agent, "side_model", lambda name: SideModel(name, object(), None))
    output = StringIO()
    app = PreviewApp(
        model="p:own", runtime=runtime, console=Console(file=output, color_system=None, width=200)
    )
    return app, output


def test_several_models_run_in_parallel_and_fail_independently(monkeypatch):
    runtime = FanOutRuntime()
    app, output = fan_out_app(monkeypatch, runtime)
    failures = []
    app.asides.on_failure = lambda aside, error: failures.append(aside.model)

    async def run():
        await app.start_aside("second opinions?", ["p:own", "p:broken", "q:other"])
        await asyncio.sleep(0)
        # One side question per model, all running at once.
        assert app.asides.running == 3
        # The conversation's own model takes the default, cache-sharing path.
        assert runtime.calls == [None, "p:broken", "q:other"]
        assert [(a.model, a.label) for a in app.asides.items] == [
            ("p:own", "own"),
            ("p:broken", "broken"),
            ("q:other", "other"),
        ]
        runtime.release.set()
        await asyncio.sleep(0.05)
        await app.asides.close()

    asyncio.run(run())
    statuses = {aside.model: aside.status for aside in app.asides.items}
    assert statuses == {"p:own": "answered", "p:broken": "failed", "q:other": "answered"}
    assert app.asides.items[2].answer == "q:other says hi"
    assert failures == ["p:broken"]
    text = " ".join(output.getvalue().split())
    assert "Asking beside the conversation on p:broken, q:other" in text
    assert "without the conversation's prompt cache" in text


def test_the_conversations_model_named_explicitly_is_the_default_path(monkeypatch):
    runtime = FanOutRuntime()
    app, output = fan_out_app(monkeypatch, runtime)

    async def run():
        await app.start_aside("why?", ["p:own"])
        await asyncio.sleep(0)
        runtime.release.set()
        await app.asides.close()

    asyncio.run(run())
    assert runtime.calls == [None]
    assert "prompt cache" not in output.getvalue()


def test_an_unusable_model_fails_the_command_before_anything_starts(monkeypatch):
    from pcode import agent

    def refuse(name):
        if name == "bad:model":
            raise ValueError(f"Cannot use {name}: Unknown model")
        return SideModel(name, object(), None)

    monkeypatch.setattr(agent, "side_model", refuse)
    app = PreviewApp(model="p:own", runtime=FanOutRuntime(), console=Console(file=StringIO()))

    async def run():
        with pytest.raises(ValueError, match="Cannot use bad:model"):
            await app.start_aside("why?", ["q:fine", "bad:model"])

    asyncio.run(run())
    assert app.asides.items == []


def test_btw_command_parses_models_and_explains_its_syntax():
    app = PreviewApp(
        model="test:local",
        runtime=AgentRuntime(Agent(TestModel())),
        console=Console(file=StringIO()),
    )
    assert app.registry.dispatch("/btw $a:x $b:y $a:x is it right?")
    assert app.aside_requested == (["a:x", "b:y"], "is it right?")
    with pytest.raises(ValueError, match=r"Usage: /btw \[\$PROVIDER:MODEL \.\.\.\] QUESTION"):
        app.registry.dispatch("/btw $a:x")
    # Bare /btw still opens the viewer.
    app.asides.items.append(Aside(question="earlier"))
    app.aside_requested = None
    assert app.registry.dispatch("/btw")
    assert app.aside_view_requested
    assert app.aside_requested is None


def test_the_model_labels_running_rows_and_the_viewer():
    from pcode.aside_ui import AsideBrowser, row
    from pcode.ui import Activity

    asides = Asides()
    labeled = Aside(question="why?", model="q:other", label="other", activity="Reading x")
    asides.items.extend([labeled, Aside(question="own?")])
    activity = Activity()
    activity.asides = asides.items
    rows = [text for _, text in activity.aside_rows("⠋", 80)]
    assert rows[0].startswith("⠋ btw · other · why? · Reading x · ")
    assert rows[1].startswith("⠋ btw · own? · ")
    assert row(labeled).startswith("[other] why?")
    assert row(asides.items[1]).startswith("own?")
    browser = AsideBrowser(asides, selected=labeled.id, output=None, input=None)
    assert "on q:other" in browser.detail.text(80)
