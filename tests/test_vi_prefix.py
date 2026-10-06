"""The optional normal-mode leader shares actions without taking editing keys."""

import asyncio

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.output import DummyOutput

from pcode.commands import CommandRegistry
from pcode.model_ui import ModelPicker
from pcode.preferences import SETTINGS, parse_vi_key_prefix, save_preferences
from pcode.ui import create_prompt


@pytest.mark.parametrize(
    "value,keys", [("off", ()), ("<space>", (" ",)), (",", (",",)), ("\\", ("\\",)), ("l", ("l",))]
)
def test_parse_vi_key_prefix(value, keys):
    assert parse_vi_key_prefix(value) == keys
    SETTINGS["vi_key_prefix"].validate("vi_key_prefix", value)


@pytest.mark.parametrize("value", ["", " ", "\t", "\x1b", "jj", "ctrl+b", "<enter>"])
def test_vi_key_prefix_rejects_non_printable_or_multiple_keys(value):
    with pytest.raises(ValueError, match="vi_key_prefix"):
        SETTINGS["vi_key_prefix"].validate("vi_key_prefix", value)


@pytest.mark.parametrize("prefix,global_keys", [("ctrl+b", "\x02l"), ("ctrl", "\x0c")])
@pytest.mark.parametrize("vi_prefix,leader", [("<space>", " "), (",", ","), ("\\", "\\")])
def test_vi_leader_reuses_model_action_and_keeps_global_shortcut(
    prefix, global_keys, vi_prefix, leader
):
    save_preferences(vi_key_prefix=vi_prefix)

    async def run():
        with create_pipe_input() as pipe:
            snapshots = []

            def model():
                snapshots.append((prompt.default_buffer.text, prompt.app.vi_state.input_mode))

            prompt = create_prompt(
                CommandRegistry(),
                vi_mode=True,
                key_prefix=prefix,
                on_model=model,
                input=pipe,
                output=DummyOutput(),
            )
            pipe.send_text("draft\x1b" + leader + "l" + global_keys + "a!\r")
            assert await asyncio.wait_for(prompt.prompt_async(), 3) == "draft!"
            assert snapshots == [("draft", InputMode.NAVIGATION)] * 2

    asyncio.run(run())


@pytest.mark.parametrize(
    "vi_mode,vi_prefix,keys,expected",
    [
        (True, "<space>", "a l", "a l"),  # Insert mode.
        (False, "<space>", "a l", "a l"),  # Emacs mode.
        (True, "off", "abcd\x1b0 l", "abcd"),
        (True, "<space>", "abcd\x1b0d l", "bcd"),  # Operator pending.
        (True, "<space>", "abcd\x1b0v l\x1b", "abcd"),  # Visual selection.
        (True, "<space>", "abcd\x1b0R l", " lcd"),  # Replace mode.
        (True, "<space>", "a b\x1b0f l", "a b"),  # Character-find argument.
        (True, "<space>", "\x1b[200~a l\x1b[201~", "a l"),
        (True, "<space>", "abcd\x1b0\x1b[200~ l\x1b[201~", " labcd"),
    ],
)
def test_vi_leader_leaves_other_input_alone(vi_mode, vi_prefix, keys, expected):
    save_preferences(vi_key_prefix=vi_prefix)

    async def run():
        with create_pipe_input() as pipe:
            calls = []
            prompt = create_prompt(
                CommandRegistry(),
                vi_mode=vi_mode,
                key_prefix="ctrl+b",
                on_model=lambda: calls.append("model"),
                input=pipe,
                output=DummyOutput(),
            )
            pipe.send_text(keys + "\r")
            assert await asyncio.wait_for(prompt.prompt_async(), 3) == expected
            assert not calls

    asyncio.run(run())


@pytest.mark.parametrize("prefix", ["ctrl+b", "ctrl"])
@pytest.mark.parametrize("cancel", [" ", "\x1b", "\x03"])
def test_vi_leader_can_be_cancelled_without_editing(prefix, cancel):
    save_preferences(vi_key_prefix="<space>")

    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                CommandRegistry(),
                vi_mode=True,
                key_prefix=prefix,
                input=pipe,
                output=DummyOutput(),
            )
            pipe.send_text("draft\x1b " + cancel + "a!\r")
            assert await asyncio.wait_for(prompt.prompt_async(), 3) == "draft!"
            assert not prompt.shortcuts.visible

    asyncio.run(run())


def test_vi_leader_menu_labels_unknown_keys_and_choices():
    save_preferences(vi_key_prefix="<space>")

    async def run():
        with create_pipe_input() as pipe:
            thinking = []
            prompt = create_prompt(
                CommandRegistry(),
                vi_mode=True,
                key_prefix="ctrl",
                on_thinking=thinking.append,
                input=pipe,
                output=DummyOutput(),
            )

            async def feed():
                pipe.send_text("draft\x1b z")
                while not prompt.shortcuts.message:
                    await asyncio.sleep(0.01)
                assert prompt.shortcuts.pending
                assert prompt.shortcuts.summary() == "Space …"
                assert prompt.shortcuts.message == "No binding for 'z'"
                assert prompt.default_buffer.text == "draft"
                assert ("t", "Select thinking visibility") in prompt.shortcuts.hint_rows()
                # Existing multi-step choices use exactly the same menu.
                pipe.send_text("to\r")

            assert (
                await asyncio.wait_for(
                    prompt.prompt_async(pre_run=lambda: prompt.app.create_background_task(feed())),
                    3,
                )
                == "draft"
            )
            assert thinking == ["off"]

    asyncio.run(run())


def test_letter_leader_reserves_repeat_but_not_the_global_menu_or_choices():
    save_preferences(vi_key_prefix="t")

    async def run():
        with create_pipe_input() as pipe:
            thinking = []
            prompt = create_prompt(
                CommandRegistry(),
                vi_mode=True,
                key_prefix="ctrl+b",
                on_thinking=thinking.append,
                input=pipe,
                output=DummyOutput(),
            )
            # Double t cancels. Global-prefix t must still select thinking.
            pipe.send_text("draft\x1btt\x02to\r")
            assert await asyncio.wait_for(prompt.prompt_async(), 3) == "draft"
            assert thinking == ["off"]

    asyncio.run(run())


@pytest.mark.parametrize("prefix,global_thinking", [("ctrl+b", "\x02t"), ("ctrl", "\x14")])
@pytest.mark.parametrize("vi_prefix,leader", [("<space>", " "), ("o", "o")])
def test_repeating_vi_leader_cancels_its_chooser_but_not_global_choices(
    prefix, global_thinking, vi_prefix, leader
):
    save_preferences(vi_key_prefix=vi_prefix)

    async def run():
        with create_pipe_input() as pipe:
            thinking = []
            prompt = create_prompt(
                CommandRegistry(),
                vi_mode=True,
                key_prefix=prefix,
                on_thinking=thinking.append,
                input=pipe,
                output=DummyOutput(),
            )
            pipe.send_text("draft\x1b" + leader + "t" + leader + global_thinking + "oa!\r")
            assert await asyncio.wait_for(prompt.prompt_async(), 3) == "draft!"
            # The alias's chooser was cancelled; only the global one selected Off.
            assert thinking == ["off"]

    asyncio.run(run())


def test_vi_leader_can_close_its_full_help_without_hiding_global_actions():
    save_preferences(vi_key_prefix="t")

    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                CommandRegistry(),
                vi_mode=True,
                key_prefix="ctrl+b",
                input=pipe,
                output=DummyOutput(),
            )

            async def feed():
                pipe.send_text("draft\x1bt\x1bOP")  # F1 opens full help from the vi menu.
                while not prompt.shortcuts.browsing:
                    await asyncio.sleep(0.01)
                assert ("Ctrl+B t", "Select thinking visibility") in prompt.shortcuts.hint_rows()
                pipe.send_text("ta!\r")

            assert (
                await asyncio.wait_for(
                    prompt.prompt_async(pre_run=lambda: prompt.app.create_background_task(feed())),
                    3,
                )
                == "draft!"
            )

    asyncio.run(run())


@pytest.mark.parametrize("leader", ["t", "T", "ß"])
def test_vi_leader_label_preserves_printable_case(leader):
    save_preferences(vi_key_prefix=leader)
    with create_pipe_input() as pipe:
        prompt = create_prompt(
            CommandRegistry(),
            vi_mode=True,
            input=pipe,
            output=DummyOutput(),
        )
        with set_app(prompt.app):
            prompt.app.vi_state.input_mode = InputMode.NAVIGATION
            assert prompt.shortcuts.summary() == f"{leader} Keybindings"


def test_vi_prefix_summary_and_strict_normal_mode_filter():
    save_preferences(vi_key_prefix="<space>")
    with create_pipe_input() as pipe:
        prompt = create_prompt(
            CommandRegistry(),
            vi_mode=True,
            key_prefix="ctrl+b",
            input=pipe,
            output=DummyOutput(),
        )
        with set_app(prompt.app):
            assert prompt.shortcuts.summary() == "^B Keybindings"
            prompt.app.vi_state.temporary_navigation_mode = True
            assert not prompt.shortcuts.vi_leader_enabled()
            prompt.app.vi_state.temporary_navigation_mode = False
            prompt.app.vi_state.input_mode = InputMode.NAVIGATION
            assert prompt.shortcuts.summary() == "Space Keybindings"
            prompt.app.quoted_insert = True
            assert not prompt.shortcuts.vi_leader_enabled()


def test_model_popup_search_does_not_adopt_vi_prefix():
    save_preferences(editing_mode="vi", vi_key_prefix="<space>")

    async def run():
        with create_pipe_input() as pipe:
            picker = ModelPicker([], [], input=pipe, output=DummyOutput())
            pipe.send_text("a l\x03")
            assert await asyncio.wait_for(picker.app.run_async(), 3) is None
            assert picker.search.text == "a l"
            assert picker.shortcuts.vi_leader == ()

    asyncio.run(run())
