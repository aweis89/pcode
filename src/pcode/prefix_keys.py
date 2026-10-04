"""Shortcut keys behind one configurable prefix, with a which-key hint.

The ``key_prefix`` setting defaults to ``ctrl+b``. With a leader, a shortcut is
the leader and then its letter. The leader lists what it can do until an action
is selected or the menu is dismissed.

A shortcut works wherever focus is. A chord outranks the editing keys of any
search line or editor, and an open menu owns keyboard input: every binding a
surface passes through ``key_bindings`` is off until dismissal, so the letter
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


def compact_label(label: str) -> str:
    """A shortcut label short enough for an inline hint: Ctrl+S reads ^S."""
    return label.replace("Ctrl+", "^")


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
class Choice:
    key: str
    label: str
    handler: Callable[[KeyPressEvent], None]


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
        self.browsing = False
        self.help_offset = 0
        self._help_title = "Keybindings"
        self.choice_title = ""
        self.choices: tuple[Choice, ...] = ()
        self.help_provider: Callable[[], list[tuple[str, str]]] = lambda: []
        self.message = ""
        self.shortcuts: list[Shortcut] = []
        self.bindings = KeyBindings()
        self.waiting: Filter = Condition(self._waiting)
        if self.leader:
            self._bind_leader()
        self._bind_help()
        self._bind_choices()

    def _waiting(self) -> bool:
        if not self.visible:
            return False
        typed = get_app().key_processor.key_buffer
        passthrough = (
            {Keys.CPRResponse, Keys.Ignore} if self.browsing or self.choices else _PASSTHROUGH
        )
        return not typed or typed[-1].key not in passthrough

    @property
    def visible(self) -> bool:
        return self.pending or self.browsing or bool(self.choices)

    def dismiss(self) -> None:
        self.pending = self.browsing = False
        self.choices = ()
        self.choice_title = ""
        self.message = ""
        self.help_offset = 0

    def set_help(
        self, provider: Callable[[], list[tuple[str, str]]], *, title: str = "Keybindings"
    ) -> None:
        """Describe ordinary keys for the focused control without moving focus."""
        self.help_provider = provider
        self._help_title = title

    @property
    def help_title(self) -> str:
        return self.choice_title if self.choices else self._help_title

    def choose(self, title: str, choices: list[Choice]) -> None:
        """Show explicit single-key choices in the existing help overlay.

        Handlers receive the selection event, with the original focus and draft.
        """
        if not choices or any(len(choice.key) != 1 for choice in choices):
            raise ValueError("Choices need single-character keys")
        if len({choice.key for choice in choices}) != len(choices):
            raise ValueError("Choice keys must be unique")
        self.dismiss()
        self.choice_title = title
        self.choices = tuple(choices)
        get_app().invalidate()

    def _bind_choices(self) -> None:
        choosing = Condition(lambda: bool(self.choices))

        @self.bindings.add("escape", filter=choosing, eager=True)
        @self.bindings.add("c-c", filter=choosing, eager=True)
        def cancel(event: KeyPressEvent) -> None:
            self.dismiss()
            event.app.invalidate()

        @self.bindings.add(Keys.Any, filter=choosing, eager=True)
        def select(event: KeyPressEvent) -> None:
            for choice in self.choices:
                if event.key_sequence[-1].key == choice.key:
                    self.dismiss()
                    choice.handler(event)
                    event.app.invalidate()
                    return
            self.message = f"No choice for {event.data!r}"
            event.app.invalidate()

        # Default eager bindings beat Any (quoted insert and history search).
        # Claim them explicitly, along with non-keyboard input that could alter
        # the underlying draft or focus. A waiting leader still passes paste
        # and mouse events through as before; browse and choices are read-only.
        protected = Condition(lambda: self.browsing or bool(self.choices))

        def ignore(event: KeyPressEvent) -> None:
            pass

        for key in ("c-q", "c-r", "c-s"):
            self.bindings.add(key, filter=Condition(lambda: self.visible), eager=True)(ignore)

        for key in (
            Keys.BracketedPaste,
            Keys.Vt100MouseEvent,
            Keys.WindowsMouseEvent,
            Keys.ScrollUp,
            Keys.ScrollDown,
        ):
            self.bindings.add(key, filter=protected, eager=True)(ignore)

    def _bind_help(self) -> None:
        # Do not eagerly consume the first key of a multi-key F1 leader.
        # Once that leader is waiting, F1 can still open the full help view.
        f1_leader = bool(self.leader and self.leader[0] == "f1")

        @self.bindings.add(
            "f1", filter=Condition(lambda: not f1_leader or self.visible), eager=True
        )
        def help_keys(event: KeyPressEvent) -> None:
            browsing = not self.browsing
            self.dismiss()
            self.browsing = browsing
            event.app.invalidate()

        browse = Condition(lambda: self.browsing)
        scrollable = Condition(lambda: self.visible)

        @self.bindings.add("escape", filter=browse, eager=True)
        @self.bindings.add("c-c", filter=browse, eager=True)
        def close(event: KeyPressEvent) -> None:
            self.dismiss()
            event.app.invalidate()

        @self.bindings.add(Keys.Any, filter=browse, eager=True)
        @self.bindings.add(Keys.BracketedPaste, filter=browse, eager=True)
        def ignore(event: KeyPressEvent) -> None:
            pass

        @self.bindings.add("down", filter=scrollable, eager=True)
        @self.bindings.add("pagedown", filter=scrollable, eager=True)
        def down(event: KeyPressEvent) -> None:
            self.help_offset = min(max(0, len(self.hint_rows()) - 1), self.help_offset + 1)
            event.app.invalidate()

        @self.bindings.add("up", filter=scrollable, eager=True)
        @self.bindings.add("pageup", filter=scrollable, eager=True)
        def up(event: KeyPressEvent) -> None:
            self.help_offset = max(0, self.help_offset - 1)
            event.app.invalidate()

    def _bind_leader(self) -> None:
        keys = self.bindings

        # Eager so the leader wins over whatever the key did before. Pressed
        # again while waiting, it cancels.
        @keys.add(*self.leader, eager=True)
        def lead(event: KeyPressEvent) -> None:
            pending = not self.visible
            self.dismiss()
            self.pending = pending
            event.app.invalidate()

        # Esc cancels; unknown keys keep the menu open with an error instead
        # of typing into the draft or invoking an underlying list command.
        @keys.add("escape", filter=self.waiting, eager=True)
        @keys.add("c-c", filter=self.waiting, eager=True)
        def cancel(event: KeyPressEvent) -> None:
            self.dismiss()
            event.app.invalidate()

        @keys.add(Keys.Any, filter=self.waiting, eager=True)
        def unknown(event: KeyPressEvent) -> None:
            if self.pending:
                self.message = f"No binding for {event.data!r}"
                event.app.invalidate()

    def add(self, key: str, label: str | Callable[[], str], *, filter: FilterOrBool = True):
        """Register a shortcut: ``key`` after the leader, or Ctrl+``key``.

        ``label`` describes the action clearly in the help overlay;
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
                self.bindings.add(f"c-{key}", filter=condition & ~self.waiting, eager=True)(handler)
                return handler

            @self.bindings.add(
                key, filter=Condition(lambda: self.pending) & self.waiting & condition, eager=True
            )
            def run(event: KeyPressEvent) -> None:
                self.dismiss()
                event.app.invalidate()
                handler(event)

            return handler

        return decorator

    def gate(self, bindings: KeyBindingsBase) -> KeyBindingsBase:
        """``bindings``, switched off while a shortcut, help, or choice menu is open.

        Use it for bindings that sit on a container, such as a docked editor's
        Enter and Esc, which would otherwise outrank the shortcuts.
        """
        return ConditionalKeyBindings(bindings, ~self.waiting)

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
        """The single help affordance, never a list of individual bindings."""
        key = compact_label(self.leader_label) if self.leader else "F1"
        return f"{key} {'…' if self.pending else 'Keybindings'}"

    def hint_rows(self) -> list[tuple[str, str]]:
        """The leader's which-key list: (key, what it does), then how to back out."""
        if self.choices:
            return [(choice.key, choice.label) for choice in self.choices] + [("Esc", "Cancel")]
        actions = [
            (shortcut.key if self.pending else self.label(shortcut.key), shortcut.label)
            for shortcut in self.available()
        ]
        if self.browsing:
            return self.help_provider() + actions + [("Esc / F1", "Dismiss help")]
        return actions + [("Esc", "Cancel"), ("F1", "All keys")]

    def hint_text(self) -> str:
        """The which-key list as one line, for surfaces that wrap it themselves."""
        listed = " · ".join(f"{key} {label}" for key, label in self.hint_rows())
        return f"{self.leader_label} … {listed}"
