"""Session titles asked of the model, and the terminal tab title that shows them."""

import asyncio
import os
from io import StringIO

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from rich.console import Console

from pcode.agent import SideModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.preferences import save_preferences
from pcode.runtime import Message
from pcode.session_naming import INSTRUCTIONS, clean_title, request_text, suggest_title
from pcode.sessions import SavedSession, list_sessions
from pcode.terminal_notify import RESTORE_TITLE, SAVE_TITLE, TabTitle, title


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("Fix billing outage", "Fix billing outage"),
        ('Title: "Fix billing outage."', "Fix billing outage"),
        ("\n\n**Session name:** Redis queue retries\nsecond line", "Redis queue retries"),
        ("`Theme tests`!", "Theme tests"),
        ("Bad \x1b[31m  escape", "Bad [31m escape"),
        ("", ""),
        ("  \n ", ""),
    ],
)
def test_answers_are_cleaned_to_one_short_line(answer, expected):
    assert clean_title(answer) == expected


def test_a_long_answer_is_cut_to_a_list_row():
    cleaned = clean_title("word " * 40)
    assert len(cleaned) <= 60 and cleaned.startswith("word word")


def test_the_request_carries_the_first_exchange_trimmed():
    text = request_text("x" * 5000, "z" * 5000)
    assert text.count("x") == 2000 and text.count("z") == 1500
    assert "assistant's reply" not in request_text("Just a prompt", "  ")


def test_suggest_title_asks_the_sessions_own_model_at_low_effort(monkeypatch):
    asked = []
    model = TestModel(custom_output_text='Title: "Fix the parser."')

    def side_model(name, effort=""):
        asked.append((name, effort))
        return SideModel(name, model, None)

    monkeypatch.setattr("pcode.agent.side_model", side_model)
    assert asyncio.run(suggest_title("anthropic:x", "Fix the parser", "Done")) == "Fix the parser"
    assert asked == [("anthropic:x", "low")]


def test_a_claude_model_answers_from_a_process_it_does_not_keep(monkeypatch):
    from pcode.claude_sdk.model import KEEP_WARM_SETTING
    from pcode.claude_sdk.workspace import CWD_SETTING

    seen = {}
    model = TestModel(custom_output_text="Claude title")
    # A `claude:` model names its system "claude"; the stand-in only needs that.
    monkeypatch.setattr(TestModel, "system", property(lambda self: "claude"))

    def side_model(name, effort=""):
        return SideModel(name, model, {"anthropic_effort": "low"})

    monkeypatch.setattr("pcode.agent.side_model", side_model)

    async def run():
        import pydantic_ai.direct

        original = pydantic_ai.direct.model_request_stream

        def wrapped(chosen, messages, *, model_settings=None):
            seen["settings"] = model_settings
            seen["instructions"] = messages[0].instructions
            return original(chosen, messages)

        monkeypatch.setattr(pydantic_ai.direct, "model_request_stream", wrapped)
        return await suggest_title("claude:x", "prompt", workspace="/work")

    assert asyncio.run(run()) == "Claude title"
    expected = {"anthropic_effort": "low", CWD_SETTING: "/work", KEEP_WARM_SETTING: False}
    assert seen["settings"] == expected
    assert seen["instructions"] == INSTRUCTIONS


def saved_app(tmp_path, *, prompt="Fix the parser", reply="Patched parser.py"):
    root = tmp_path / "sessions"
    app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
    app.model = "test:local"
    app.runtime = AgentRuntime(Agent("test"), None)
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.append("turn_started", prompt=prompt)
    saved.event(Message(reply))
    saved.append("turn_completed")
    saved.save_info()
    app.runtime.session = saved
    return app, saved, root


def test_a_title_is_saved_without_moving_the_session_in_resume(tmp_path, monkeypatch):
    save_preferences(session_naming="on")
    asked = []

    async def suggest(model, prompt, reply="", *, workspace=None):
        asked.append((model, prompt, reply, workspace))
        return "Parser fix"

    monkeypatch.setattr("pcode.session_naming.suggest_title", suggest)
    app, saved, root = saved_app(tmp_path)
    updated = saved.info.updated

    async def run():
        app.controller.start_naming()
        await app.controller.naming_task
        # Once per session, whatever the outcome.
        app.controller.start_naming()
        assert app.controller.naming_task.done()

    try:
        asyncio.run(run())
        assert asked == [("test:local", "Fix the parser", "Patched parser.py", tmp_path)]
        (info,) = list_sessions(root)
        assert (info.title, info.updated) == ("Parser fix", updated)
        assert app.controller.session_title() == "Parser fix"
        assert dict(app.controller.session_overview())["Title"] == "Parser fix"
        # /rename wins, and clearing it falls back to the title.
        app.controller.rename("Mine")
        assert app.controller.session_title() == "Mine"
        app.controller.rename("-")
        assert app.controller.session_title() == "Parser fix"
    finally:
        saved.close()


@pytest.mark.parametrize("case", ["off", "named", "failing"])
def test_no_title_when_off_named_or_failing(tmp_path, monkeypatch, case):
    save_preferences(session_naming="off" if case == "off" else "on")
    calls = []

    async def suggest(model, prompt, reply="", *, workspace=None):
        calls.append(prompt)
        raise RuntimeError("provider down")

    monkeypatch.setattr("pcode.session_naming.suggest_title", suggest)
    app, saved, root = saved_app(tmp_path)
    if case == "named":
        app.controller.rename("Already named")

    async def run():
        app.controller.start_naming()
        if app.controller.naming_task is not None:
            await app.controller.naming_task

    try:
        asyncio.run(run())
        assert calls == (["Fix the parser"] if case == "failing" else [])
        assert list_sessions(root)[0].title is None
    finally:
        saved.close()


def test_a_title_arriving_after_rename_is_dropped(tmp_path, monkeypatch):
    save_preferences(session_naming="on")
    app, saved, root = saved_app(tmp_path)

    async def suggest(model, prompt, reply="", *, workspace=None):
        app.controller.rename("Typed meanwhile")
        return "Late title"

    monkeypatch.setattr("pcode.session_naming.suggest_title", suggest)

    async def run():
        app.controller.start_naming()
        await app.controller.naming_task

    try:
        asyncio.run(run())
        (info,) = list_sessions(root)
        assert (info.name, info.title) == ("Typed meanwhile", None)
    finally:
        saved.close()


async def until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_new_cancels_a_title_request_and_reopening_asks_again(tmp_path, monkeypatch):
    save_preferences(session_naming="on")
    app, saved, root = saved_app(tmp_path)
    started = []

    async def suggest(model, prompt, reply="", *, workspace=None):
        started.append(prompt)
        await asyncio.Event().wait()  # Answers only when cancelled.

    monkeypatch.setattr("pcode.session_naming.suggest_title", suggest)

    async def run():
        app.controller.start_naming()
        task = app.controller.naming_task
        await until(lambda: started)
        app.controller.new("")
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        # Opened again in this process, the session is asked again.
        app.runtime.session = saved
        app.controller.start_naming()
        await until(lambda: len(started) == 2)
        await app.controller.stop_naming()
        assert app.controller.naming_task.cancelled()

    try:
        asyncio.run(run())
        assert started == ["Fix the parser", "Fix the parser"]
        assert list_sessions(root)[0].title is None
    finally:
        saved.close()


def read_all(fd: int) -> str:
    os.set_blocking(fd, False)
    try:
        return os.read(fd, 65536).decode()
    except BlockingIOError:
        return ""


def test_tab_title_follows_the_name_and_restores_the_shells():
    current = {"name": ""}
    reader, writer = os.pipe()
    try:
        tab = TabTitle(lambda: current["name"], writer)
        tab.tick()
        assert read_all(reader) == ""  # No name yet: the shell keeps its title.
        current["name"] = "Fix\x07 the\nparser"
        tab.tick()
        assert read_all(reader) == SAVE_TITLE + title("Fix the parser")
        assert title("Fix the parser") == "\x1b]0;Fix the parser\x07"
        tab.tick()
        assert read_all(reader) == ""  # Unchanged: nothing resent.
        current["name"] = "Mine"
        tab.tick()
        assert read_all(reader) == title("Mine")
        current["name"] = ""  # /new: the shell's title comes back, saved again.
        tab.tick()
        assert read_all(reader) == RESTORE_TITLE + SAVE_TITLE
        current["name"] = "Next"
        tab.tick()
        tab.close()
        assert read_all(reader) == title("Next") + RESTORE_TITLE
        tab.close()
        assert read_all(reader) == ""
    finally:
        os.close(reader)
        os.close(writer)


def test_tab_title_sends_nothing_when_off_or_not_a_terminal():
    reader, writer = os.pipe()
    try:
        for mode in ("off", "on"):  # A pipe is no terminal either.
            asyncio.run(TabTitle(lambda: "Name", writer, mode).run())
        assert read_all(reader) == ""
    finally:
        os.close(reader)
        os.close(writer)
