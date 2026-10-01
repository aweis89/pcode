"""Shortcut keys behind one configurable prefix, with a which-key hint.

The ``key_prefix`` setting is ``ctrl`` by default, which makes each shortcut a
Ctrl chord such as Ctrl+S. Set to a leader such as ``ctrl+p``, a shortcut is
the leader and then its letter, and the leader lists what it can do until the
next key arrives.

A shortcut works wherever focus is. A chord outranks the editing keys of any
search line or editor, and a waiting leader owns the next key: every binding a
surface passes through ``key_bindings`` is off until then, so the letter
reaches the shortcut instead of being typed.
"""

from collections.abc import Callable
from dataclasses import dataclass

from prompt_toolkit.application import get_app
from prompt_toolkit.filters import Condition, Filter, FilterOrBool, to_filter
from prompt_toolkit.key_binding import (
    ConditionalKeyBindings,
    KeyBindings,
    KeyBindingsBase,
    merge_key_bindings,
)
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys

from pcode.preferences import RESERVED_CHORDS, SETTINGS, load_preferences, parse_key_prefix

# Input that is not a keystroke keeps its own handler while a leader waits:
# a swallowed cursor-position report would stall the renderer, and a paste
# or a click is not an answer to "which shortcut?".
_PASSTHROUGH = frozenset(
    {
        Keys.CPRResponse,
        Keys.Vt100MouseEvent,
        Keys.WindowsMouseEvent,
        Keys.BracketedPaste,
        Keys.ScrollUp,
        Keys.ScrollDown,
        Keys.SIGINT,
        Keys.Ignore,
    }
)
# A shortcut's key must also make a Ctrl chord a terminal can send, so the
# same letter works under either prefix. Ctrl+Shift+N arrives as Ctrl+N, so
# capitals are out.
_CHORDABLE = frozenset("abcdefghijklmnopqrstuvwxyz@]\\^_")
_NAMES = {"@": "Space"}


def key_label(key: str) -> str:
    """How a prompt_toolkit key name reads to a person: c-p is Ctrl+P, f2 is F2."""
    if key.startswith("c-"):
        rest = key[2:]
        return f"Ctrl+{_NAMES.get(rest, rest.upper())}"
    return key.upper()


def configured_prefix() -> str:
    return load_preferences().get("key_prefix", SETTINGS["key_prefix"].default)


def _leader_label(leader: tuple[str, ...]) -> str:
    return " ".join(key_label(key) for key in leader)


def _shortcut_label(leader: tuple[str, ...], key: str) -> str:
    return f"{_leader_label(leader)} {key}" if leader else key_label(f"c-{key}")


def shortcut_label(key: str, prefix: str | None = None) -> str:
    """How to press shortcut ``key`` under ``prefix``, by default the saved one.

    For text written once at launch, alongside the prompt reading the same
    setting; a running prompt's own ``PrefixKeys.label`` is exact after that.
    """
    return _shortcut_label(parse_key_prefix(configured_prefix() if prefix is None else prefix), key)


@dataclass(frozen=True)
class Shortcut:
    key: str
    # A callable for a toggle whose name follows its state, read per render.
    title: str | Callable[[], str]
    handler: Callable[[KeyPressEvent], None]
    # Whether the shortcut applies right now; it is hidden and inert otherwise.
    filter: Filter

    @property
    def label(self) -> str:
        return self.title() if callable(self.title) else self.title


class PrefixKeys:
    """The shortcuts of one surface (the prompt, or a popup) and their prefix.

    Register each with ``add``, then build the surface's key bindings with
    ``key_bindings`` so a waiting leader can switch the rest off. ``prefix``
    defaults to the saved ``key_prefix``, read once, here.
    """

    def __init__(self, prefix: str | None = None) -> None:
        self.leader = parse_key_prefix(configured_prefix() if prefix is None else prefix)
        self.pending = False
        self.shortcuts: list[Shortcut] = []
        self.bindings = KeyBindings()
        self.waiting: Filter = Condition(self._waiting)
        if self.leader:
            self._bind_leader()

    def _waiting(self) -> bool:
        if not self.pending:
            return False
        typed = get_app().key_processor.key_buffer
        return not typed or typed[-1].key not in _PASSTHROUGH

    def _bind_leader(self) -> None:
        keys = self.bindings

        # Eager so the leader wins over whatever the key did before. Pressed
        # again while waiting, it cancels.
        @keys.add(*self.leader, eager=True)
        def lead(event: KeyPressEvent) -> None:
            self.pending = not self.pending
            event.app.invalidate()

        # Anything that is not a shortcut ends the wait without doing what it
        # does otherwise: a stray letter is neither typed nor a list command.
        @keys.add("escape", filter=self.waiting, eager=True)
        @keys.add("c-c", filter=self.waiting, eager=True)
        @keys.add(Keys.Any, filter=self.waiting, eager=True)
        def cancel(event: KeyPressEvent) -> None:
            self.pending = False
            event.app.invalidate()

    def add(self, key: str, label: str | Callable[[], str], *, filter: FilterOrBool = True):
        """Register a shortcut: ``key`` after the leader, or Ctrl+``key``.

        ``label`` names it in footers and the leader's hint, so keep it short;
        a callable is read each time, for a toggle that says what it does next.
        """
        if key not in _CHORDABLE or f"c-{key}" in RESERVED_CHORDS:
            raise ValueError(f"{key!r} cannot be a shortcut: it has no free Ctrl chord")
        if any(shortcut.key == key for shortcut in self.shortcuts):
            raise ValueError(f"{key!r} is already a shortcut here")
        condition = to_filter(filter)

        def decorator(handler: Callable[[KeyPressEvent], None]):
            self.shortcuts.append(Shortcut(key, label, handler, condition))
            if not self.leader:
                # Eager, so a chord that starts a longer default binding
                # (Ctrl+X in Emacs mode) fires at once instead of after a pause.
                self.bindings.add(f"c-{key}", filter=condition, eager=True)(handler)
                return handler

            @self.bindings.add(key, filter=self.waiting & condition, eager=True)
            def run(event: KeyPressEvent) -> None:
                self.pending = False
                event.app.invalidate()
                handler(event)

            return handler

        return decorator

    def gate(self, bindings: KeyBindingsBase) -> KeyBindingsBase:
        """``bindings``, switched off while the leader waits for its key.

        Use it for bindings that sit on a container, such as a docked editor's
        Enter and Esc, which would otherwise outrank the shortcuts.
        """
        return ConditionalKeyBindings(bindings, ~self.waiting) if self.leader else bindings

    def key_bindings(self, bindings: KeyBindingsBase) -> KeyBindingsBase:
        """A surface's own bindings, gated, followed by its shortcuts."""
        return merge_key_bindings([self.gate(bindings), self.bindings])

    @property
    def leader_label(self) -> str:
        return _leader_label(self.leader)

    def label(self, key: str) -> str:
        """How to press shortcut ``key``: Ctrl+Y, or Ctrl+P y after a leader."""
        return _shortcut_label(self.leader, key)

    def available(self) -> list[Shortcut]:
        return [shortcut for shortcut in self.shortcuts if shortcut.filter()]

    def summary(self) -> str:
        """One line naming the shortcuts that apply now, for a footer."""
        shortcuts = self.available()
        if self.leader:
            listed = " · ".join(f"{shortcut.key} {shortcut.label}" for shortcut in shortcuts)
            return f"{self.leader_label}, then: {listed}" if listed else ""
        return " · ".join(f"{self.label(shortcut.key)} {shortcut.label}" for shortcut in shortcuts)

    def hint_rows(self) -> list[tuple[str, str]]:
        """The leader's which-key list: (key, what it does), then how to back out."""
        return [(shortcut.key, shortcut.label) for shortcut in self.available()] + [
            ("Esc", "Cancel")
        ]

    def hint_text(self) -> str:
        """The which-key list as one line, for surfaces that wrap it themselves."""
        listed = " · ".join(f"{key} {label}" for key, label in self.hint_rows())
        return f"{self.leader_label} … {listed}"
