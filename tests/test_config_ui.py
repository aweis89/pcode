import asyncio
from contextlib import asynccontextmanager

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pcode import config, preferences
from pcode.config_ui import ConfigBrowser


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@asynccontextmanager
async def browser():
    calls = []

    def save(args):
        calls.append(args)
        return config.configure(args)

    with create_pipe_input() as pipe:
        ui = ConfigBrowser(save=save, input=pipe, output=DummyOutput())
        task = asyncio.create_task(ui.run())
        try:
            await wait_for(lambda: ui.app.is_running)
            yield ui, pipe, calls
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_search_navigation_and_description():
    async def run():
        async with browser() as (ui, pipe, calls):
            pipe.send_text("\x1b[B")
            await wait_for(lambda: ui.selected == 1)
            pipe.send_text("\x1b[6~")
            await wait_for(lambda: ui.selected > 1)
            pipe.send_text("tool_max_lines")
            await wait_for(lambda: ui.matches == ["tool_max_lines"])
            assert ui.selected == 0
            assert "Saved effective:" in ui.details()
            assert " · default" in str(ui.fragments())
            pipe.send_text("\x01\x0b" + preferences.SETTINGS["tool_max_lines"].description)
            await wait_for(lambda: "tool_max_lines" in ui.matches and " " in ui.search.text)
            pipe.send_text("\x01\x0bnot-a-real-setting-xyz")
            await wait_for(lambda: not ui.matches)
            assert "No matching settings" in str(ui.fragments())
            pipe.send_text("\r\x03")
            await wait_for(lambda: not ui.app.is_running)
            assert not calls

    asyncio.run(run())


def test_enum_save_typed_ahead_and_cancel():
    async def run():
        async with browser() as (ui, pipe, calls):
            key = "show_thinking"
            choices = preferences.SETTINGS[key].choices
            initial = choices.index(preferences.SETTINGS[key].default)
            next_index = min(initial + 1, len(choices) - 1)
            pipe.send_text(key + "\r\x1b[B\r")
            await wait_for(lambda: len(calls) == 1 and not ui.editing)
            assert calls == [["set", key, choices[next_index]]]
            assert ui.app.is_running
            assert ui.effective(key) == (choices[next_index], "user")
            pipe.send_text("\r\x1b[A\x1b")
            await wait_for(lambda: ui.message.startswith("Edit cancelled"))
            assert not ui.editing
            assert len(calls) == 1
            assert ui.app.layout.has_focus(ui.search)

    asyncio.run(run())


@pytest.mark.parametrize("text", ["--label 'two words'", ""])
def test_text_preserves_spaces_and_empty_values(text):
    async def run():
        async with browser() as (ui, pipe, calls):
            key = next(k for k, s in preferences.SETTINGS.items() if s.arguments)
            pipe.send_text(key + "\r\x01\x0b" + text + "\r")
            await wait_for(lambda: len(calls) == 1 and not ui.editing)
            assert calls == [["set", key, text]]
            assert preferences.read_preferences()[key] == text
            assert ui.app.layout.has_focus(ui.search)
            if not text:
                assert 'Saved effective: ""' in ui.details()

    asyncio.run(run())


def test_numeric_named_choices_remain_free_input_and_validate():
    async def run():
        async with browser() as (ui, pipe, calls):
            key = next(
                k
                for k, s in preferences.SETTINGS.items()
                if s.choices and (s.positive_integer or s.whole_number)
            )
            pipe.send_text(key + "\r\x01\x0bnope\r")
            await wait_for(lambda: "must be" in ui.message)
            assert ui.editing
            assert not ui.choices
            assert ui.value.text == "nope"
            assert not calls
            assert "Suggestions:" in ui.details()
            pipe.send_text("\x01\x0b42\r")
            await wait_for(lambda: len(calls) == 1 and not ui.editing)
            assert calls == [["set", key, "42"]]

    asyncio.run(run())


@pytest.mark.parametrize(
    "prefix,toggle,reset",
    [
        ("ctrl", "\x14", "\x12"),
        ("ctrl+x", "\x18t", "\x18r"),
    ],
)
def test_scopes_inheritance_and_reset(monkeypatch, tmp_path, prefix, toggle, reset):
    monkeypatch.setattr("pcode.prefix_keys.configured_prefix", lambda: prefix)
    preferences.set_project_root(tmp_path)
    config.configure(["set", "model", "provider:user-model"])
    config.configure(["project", "set", "model", "provider:project-model"])

    async def run():
        async with browser() as (ui, pipe, calls):
            pipe.send_text("model")
            await wait_for(lambda: ui.search.text == "model")
            # Exact name first is not required; select it from the filtered list.
            ui.selected = ui.matches.index("model")
            assert ui.effective("model") == ("provider:project-model", "project")
            assert 'user override: "provider:user-model"' in ui.details()
            pipe.send_text(toggle)
            await wait_for(lambda: ui.scope == "project")
            ui.search.text = ""
            assert not set(ui.matches) & preferences.USER_ONLY
            assert set(ui.matches) == set(config.listed_settings()) - preferences.USER_ONLY
            ui.search.text = "model"
            ui.selected = ui.matches.index("model")
            assert 'project override: "provider:project-model"' in ui.details()
            pipe.send_text(reset)
            await wait_for(lambda: len(calls) == 1)
            assert calls == [["project", "unset", "model"]]
            assert ui.effective("model") == ("provider:user-model", "user")
            assert "none (inherited)" in ui.details()
            pipe.send_text("\r\x01\x0bprovider:new\r")
            await wait_for(lambda: len(calls) == 2 and not ui.editing)
            assert calls[-1] == ["project", "set", "model", "provider:new"]
            assert ui.effective("model") == ("provider:new", "project")
            pipe.send_text(toggle)
            await wait_for(lambda: ui.scope == "user")
            ui.selected = ui.matches.index("model")
            pipe.send_text(reset)
            await wait_for(lambda: len(calls) == 3)
            assert calls[-1] == ["unset", "model"]
            assert ui.effective("model") == ("provider:new", "project")
            assert "model" not in preferences.read_preferences()

    asyncio.run(run())


def test_no_workspace_scope_disabled_and_plain_text_is_not_reset():
    async def run():
        async with browser() as (ui, pipe, calls):
            assert ui.project_path is None
            pipe.send_text("\x14r")
            await wait_for(lambda: ui.search.text == "r")
            assert ui.scope == "user"
            assert not calls

    asyncio.run(run())


def test_saved_values_ignore_invalid_and_user_only_project_overrides(tmp_path):
    preferences.set_project_root(tmp_path)
    preferences.update_preferences({"model": "user-model", "tool_max_lines": "23"})
    preferences.update_preferences(
        {"model": "bad value", "tool_max_lines": "bad", "extension_dirs": "/ignored"},
        path=preferences.project_preferences_path(),
    )
    with create_pipe_input() as pipe:
        ui = ConfigBrowser(save=config.configure, input=pipe, output=DummyOutput())
        assert ui.effective("tool_max_lines") == ("23", "user")
        assert config.configure(["get", "tool_max_lines"]) == "23"
        assert "tool_max_lines = 23 (default 3, from user)" in config.configure(["diff"])
        assert preferences.load_preferences()["tool_max_lines"] == "23"
        assert ui.effective("extension_dirs")[1] == "default"
        ui.search.text = "tool_max_lines"
        assert "Layout setting; applies immediately when saved" in ui.details()
        ui.search.text = "model"
        ui.selected = ui.matches.index("model")
        assert "Saved default; may require next launch" in ui.details()


def test_overrides_filter_includes_default_values_and_tracks_reset(tmp_path):
    preferences.set_project_root(tmp_path)
    default = preferences.SETTINGS["tool_max_lines"].default
    preferences.update_preferences({"tool_max_lines": default})
    preferences.update_preferences(
        {"tool_glyphs": "off"}, path=preferences.project_preferences_path()
    )

    async def run():
        async with browser() as (ui, pipe, calls):
            pipe.send_text("tool_\x0f")  # Ctrl+O: overrides in the selected scope.
            await wait_for(lambda: ui.changed_only)
            assert ui.matches == ["tool_max_lines"]
            assert ui.effective("tool_max_lines") == (default, "user")
            pipe.send_text("\x14")  # Ctrl+T: project scope.
            await wait_for(lambda: ui.scope == "project")
            assert ui.matches == ["tool_glyphs"]
            pipe.send_text("\x12")  # Ctrl+R removes the project override.
            await wait_for(lambda: not ui.matches)
            assert calls == [["project", "unset", "tool_glyphs"]]
            assert ui.app.is_running
            pipe.send_text("\x0f")
            await wait_for(lambda: not ui.changed_only)
            assert "tool_glyphs" in ui.matches
            assert "tool_max_lines" in ui.matches

    asyncio.run(run())


def test_text_cancel_and_help_preserve_draft():
    async def run():
        async with browser() as (ui, pipe, calls):
            pipe.send_text("tool_max_lines\r\x01\x0b123\x1f")
            await wait_for(lambda: ui.shortcuts.browsing)
            pipe.send_text("ignored\r\x1f")
            await wait_for(lambda: not ui.shortcuts.visible)
            assert ui.value.text == "123"
            assert ui.editing
            assert not calls
            pipe.send_text("\x1b")
            await wait_for(lambda: not ui.editing)
            assert not calls
            assert "tool_max_lines" not in preferences.read_preferences()
            assert ui.app.is_running

    asyncio.run(run())


def test_save_error_keeps_edit_open():
    async def run():
        async with browser() as (ui, pipe, calls):

            def fail(args):
                raise OSError("Cannot write preferences")

            ui.save = fail
            pipe.send_text("show_thinking\r\r")
            await wait_for(lambda: ui.message == "Cannot write preferences")
            assert ui.editing
            assert ui.app.is_running
            assert not calls

    asyncio.run(run())
