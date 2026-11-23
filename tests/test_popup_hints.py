"""Every popup's key hints come from one registry (`pcode.popup_hints`)."""

import base64
import json
import os

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input import DummyInput
from prompt_toolkit.layout import Layout, Window, to_container
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from pcode import popup_hints, theme
from pcode.pick_ui import PickAction, payload
from pcode.popup_hints import (
    POPUPS,
    Hint,
    HintBar,
    Role,
    fit,
    fragments,
    hints,
    hints_apc,
    native_keycap,
    shortcut_hints,
)
from pcode.popup_surface import _OverlayWriter
from pcode.prefix_keys import PrefixKeys


def ctrl(letter: str) -> str:
    return f"Ctrl+{letter.upper()}"


def labels(found) -> list[str]:
    return [f"{hint.key} {hint.label}" for hint in found]


def test_every_popup_reads_primary_actions_navigation_leave_help_in_that_order():
    for kind in POPUPS:
        found = hints(kind, label=ctrl, help_hint=Hint("Ctrl+/", "Keys", Role.HELP))
        roles = [hint.role for hint in found]
        assert roles == sorted(roles), kind
        assert found[-1].label == "Keys", kind
        # Every popup says how to get out of it.
        assert any(hint.role is Role.LEAVE for hint in found), kind


def test_conditions_pick_the_entries_and_an_unreported_one_is_false():
    editing = hints("config", label=ctrl, state={"editing": lambda: True})
    assert labels(editing) == ["Enter Save", "Tab Details", "Esc Cancel"]
    browsing = hints("config", label=ctrl, state={"editing": lambda: False})
    assert labels(browsing)[0] == "Enter Edit" and "Esc Close" in labels(browsing)
    # Not reporting `editing` reads as not editing.
    assert labels(hints("config", label=ctrl)) == labels(browsing)


def test_a_shortcut_shows_as_pressed_under_the_prefix_and_only_while_it_works():
    shortcuts = PrefixKeys("ctrl")
    shortcuts.add("w", "Watch in preview")(lambda event: None)
    shortcuts.add("k", "Stop", filter=False)(lambda event: None)
    assert labels(shortcut_hints(shortcuts, "jobs")) == ["Ctrl+W Watch", "Esc Close", "Ctrl+/ Keys"]
    leader = PrefixKeys("ctrl+p")
    leader.add("w", "Watch in preview")(lambda event: None)
    assert labels(shortcut_hints(leader, "jobs"))[0] == "Ctrl+P w Watch"


def test_a_key_listed_twice_keeps_its_first_entry():
    found = hints("btw", label=ctrl, state={"picking": lambda: True, "listing": lambda: True})
    assert [hint.label for hint in found if hint.key == "Enter"] == ["Open link"]


def test_a_narrow_bar_drops_actions_from_the_end_but_keeps_act_leave_and_help():
    found = [
        Hint("Enter", "Resume", Role.PRIMARY),
        Hint("Ctrl+F", "Search"),
        Hint("Ctrl+R", "Prompts only"),
        Hint("Esc", "Close", Role.LEAVE),
        Hint("Ctrl+/", "Keys", Role.HELP),
    ]
    assert fit(found, 200) == found
    assert labels(fit(found, 45)) == ["Enter Resume", "Ctrl+F Search", "Esc Close", "Ctrl+/ Keys"]
    assert labels(fit(found, 5)) == ["Enter Resume", "Esc Close", "Ctrl+/ Keys"]
    assert fragment_list_to_text(fragments(found[:2])) == "Enter Resume  ^F Search"


def test_native_keycaps_are_the_ones_macos_draws():
    assert [native_keycap(key) for key in ("Enter", "Esc", "Ctrl+Y", "Cmd+R", "Ctrl+P y")] == [
        "↩",
        "esc",
        "⌃Y",
        "⌘R",
        "⌃P y",
    ]


def decoded(apc: bytes) -> list[dict]:
    body = apc.removeprefix(b"\x1b_pcode;hints;").removesuffix(b"\x1b\\")
    return json.loads(base64.b64decode(body))


def test_the_terminal_gets_drawn_keycaps_and_which_to_keep_when_narrow():
    sent = decoded(hints_apc([Hint("Enter", "Save", Role.PRIMARY), Hint("Ctrl+O", "Overrides")]))
    assert sent == [
        {"key": "↩", "label": "Save", "keep": True},
        {"key": "⌃O", "label": "Overrides", "keep": False},
    ]
    assert decoded(hints_apc([])) == []


def test_the_native_picker_gets_its_actions_as_cmd_keys():
    actions = [PickAction("r", "Rename", delete=True)]
    sent = payload([], title="Sessions", actions=actions)["hints"]
    assert [(hint["key"], hint["label"]) for hint in sent] == [
        ("↩", "Choose"),
        ("⌘R", "Rename"),
        ("⌦", "Rename"),
        ("esc", "Cancel"),
    ]


@pytest.fixture
def overlay():
    read, write = os.pipe()
    os.set_blocking(read, False)
    writer = _OverlayWriter(write)
    writer.ready = b""
    yield Vt100_Output(writer, lambda: Size(6, 60)), writer, read
    os.close(read)
    os.close(write)


def render(container, output) -> str:
    app = Application(layout=Layout(Window()), input=DummyInput(), output=output)
    screen = Screen()
    with set_app(app):
        to_container(container).write_to_screen(
            screen, MouseHandlers(), WritePosition(0, 0, 60, 1), "", False, None
        )
    return "".join(screen.data_buffer[0][column].char for column in range(60)).rstrip()


FOUND = [Hint("Enter", "Copy", Role.PRIMARY), Hint("Esc", "Cancel", Role.LEAVE)]


def test_on_a_native_overlay_the_bar_hands_its_hints_to_the_terminal(overlay, monkeypatch):
    output, writer, read = overlay
    monkeypatch.setattr(theme, "_popup_protocol", 4)
    bar = HintBar(lambda: FOUND)
    assert render(bar, output) == ""
    # Whatever the app wrote setting up, before the bar was drawn.
    writer._pending.clear()
    try:
        os.read(read, 65536)
    except BlockingIOError:
        pass
    writer.write("frame")
    writer.flush()
    assert os.read(read, 65536) == hints_apc(FOUND) + b"frame"
    # Sent once per change.
    render(bar, output)
    writer.write("frame")
    writer.flush()
    assert os.read(read, 65536) == b"frame"


def test_elsewhere_the_bar_draws_the_hints_itself(overlay, monkeypatch):
    output, writer, _ = overlay
    # An overlay from a terminal without native footers (protocol 3).
    monkeypatch.setattr(theme, "_popup_protocol", 3)
    assert render(HintBar(lambda: FOUND), output) == "Enter Copy  Esc Cancel"
    assert writer.hints == ()
    monkeypatch.setattr(theme, "_popup_protocol", 4)
    assert render(HintBar(lambda: FOUND), DummyOutput()) == "Enter Copy  Esc Cancel"


def test_the_registry_only_names_known_roles_and_keys():
    for kind, entries in POPUPS.items():
        for entry in entries:
            assert entry.label and entry.key, kind
            assert len(popup_hints.native_keycap(entry.key)) <= popup_hints.KEY_MAX, kind
