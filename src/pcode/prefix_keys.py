"""Shortcut keys behind one configurable prefix, with a which-key hint.

The ``key_prefix`` setting defaults to ``ctrl``. With a leader, a shortcut is
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
from prompt_toolkit.filters import Condition, Filter, FilterOrBool, to_filter, vi_navigation_mode
from prompt_toolkit.key_binding import (
    ConditionalKeyBindings,
    KeyBindings,
    KeyBindingsBase,
    merge_key_bindings,
)
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.keys import Keys

from pcode.preferences import (
    RESERVED_CHORDS,
    SETTINGS,
    load_preferences,
    parse_key_prefix,
    parse_vi_key_prefix,
)

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
# Opens the read-only help view everywhere; shown as Ctrl+/ (^/ when compact).
HELP_KEY = "c-_"
HELP_LABEL = "Ctrl+/"


def has_ctrl_chord(key: str) -> bool:
    return key in _CHORDABLE and f"c-{key}" not in RESERVED_CHORDS


def validate_action_key(key: str) -> None:
    if len(key) != 1 or not key.isprintable() or key.isspace():
        raise ValueError("A binding key must be one printable non-whitespace character.")


def key_label(key: str) -> str:
    """How a prompt_toolkit key name reads to a person: c-p is Ctrl+P, f2 is F2."""
    if len(key) == 1:
        return "Space" if key == " " else key
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
    # Shortcuts registered back to back under one group share a help row.
    group: str | None = None

    @property
    def label(self) -> str:
        return self.title() if callable(self.title) else self.title


class PrefixKeys:
    """The shortcuts of one surface (the prompt, or a popup) and their prefix.

    Register each with ``add``, then build the surface's key bindings with
    ``key_bindings`` so a waiting leader can switch the rest off. ``prefix``
    defaults to the saved ``key_prefix``, read once, here.
    """

    def __init__(self, prefix: str | None = None, *, vi_prefix: str = "off") -> None:
        self.leader = parse_key_prefix(configured_prefix() if prefix is None else prefix)
        # Only the main prompt opts into this alias; popup inputs keep their keys.
        self.vi_leader = parse_vi_key_prefix(vi_prefix)
        self.vi_leader_enabled = vi_navigation_mode & Condition(
            lambda: (
                get_app().vi_state.input_mode == InputMode.NAVIGATION
                and not get_app().quoted_insert
            )
        )
        # Remember which leader opened the menu, including its help/choices,
        # so repeating a vi alias can close it without hijacking other menus.
        self._pending_leader: tuple[str, ...] = ()
        self.pending = False
        self.browsing = False
        self.help_offset = 0
        self._help_title = "Keybindings"
        self.choice_title = ""
        self.choices: tuple[Choice, ...] = ()
        self.help_provider: Callable[[], list[tuple[str, str]]] = lambda: []
        self.message = ""
        self.shortcuts: list[Shortcut] = []
        self._actions: dict[str, Shortcut] = {}
        self._bound_keys: set[str] = set()
        self.bindings = KeyBindings()
        self.waiting: Filter = Condition(self._waiting)
        if self.leader or self.vi_leader:
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
        self._pending_leader = ()
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
        # Terminals send Ctrl+/ as Ctrl+_, which no leader may use.
        @self.bindings.add(HELP_KEY, eager=True)
        def help_keys(event: KeyPressEvent) -> None:
            browsing = not self.browsing
            leader = self._pending_leader
            self.dismiss()
            self.browsing = browsing
            if browsing:
                self._pending_leader = leader
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
        def bind(leader: tuple[str, ...], filter: FilterOrBool = True) -> None:
            @keys.add(*leader, filter=filter, eager=True)
            def lead(event: KeyPressEvent) -> None:
                pending = not self.visible
                self.dismiss()
                self.pending = pending
                if pending:
                    self._pending_leader = leader
                event.app.invalidate()

        if self.leader:
            bind(self.leader)
        if self.vi_leader:
            # Do not steal choices or actions from a menu opened by another
            # prefix, even when the alias itself is an action letter.
            bind(
                self.vi_leader,
                self.vi_leader_enabled
                & Condition(lambda: not self.visible or self._pending_leader == self.vi_leader),
            )

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

    def add(
        self,
        key: str,
        label: str | Callable[[], str],
        *,
        filter: FilterOrBool = True,
        group: str | None = None,
    ):
        """Register a shortcut: ``key`` after the leader, or Ctrl+``key``.

        ``label`` describes the action clearly in the help overlay;
        a callable is read each time, for a toggle that says what it does next.
        Consecutive shortcuts with the same ``group`` list as one row under
        that name, such as ``n / p  Thinking effort up / down``.
        """
        if not has_ctrl_chord(key):
            raise ValueError(f"{key!r} cannot be a shortcut: it has no free Ctrl chord")
        if any(shortcut.key == key for shortcut in self.shortcuts):
            raise ValueError(f"{key!r} is already a shortcut here")
        condition = to_filter(filter)

        def decorator(handler: Callable[[KeyPressEvent], None]):
            self.set_shortcut(Shortcut(key, label, handler, condition, group))
            return handler

        return decorator

    def set_shortcut(self, shortcut: Shortcut) -> None:
        """Replace an action in place, or add one, without replacing the keymap."""
        key = shortcut.key
        validate_action_key(key)
        old = self._actions.get(key)
        if old is None:
            self.shortcuts.append(shortcut)
        else:
            self.shortcuts[self.shortcuts.index(old)] = shortcut
        self._actions[key] = shortcut
        if key in self._bound_keys:
            return
        self._bound_keys.add(key)
        enabled = Condition(lambda: key in self._actions and self._actions[key].filter())

        def run(event: KeyPressEvent) -> None:
            action = self._actions[key]
            leader = self._pending_leader
            self.dismiss()
            event.app.invalidate()
            action.handler(event)
            # Preserve the originating leader when an action opens a chooser.
            if self.visible and not self._pending_leader:
                self._pending_leader = leader

        if not self.leader and has_ctrl_chord(key):
            self.bindings.add(f"c-{key}", filter=enabled & ~self.waiting, eager=True)(run)
        if self.leader or self.vi_leader:
            self.bindings.add(
                key,
                filter=Condition(lambda: self.pending and self._pending_leader != (key,))
                & self.waiting
                & enabled,
                eager=True,
            )(run)

    def remove(self, key: str) -> None:
        """Disable a registered action; its bindings become inactive immediately."""
        old = self._actions.pop(key, None)
        if old is not None:
            self.shortcuts.remove(old)

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
        if not self.leader and not has_ctrl_chord(key):
            return (
                _shortcut_label(self.vi_leader, key)
                if self.vi_leader
                else f"leader {key} (needs a prefix)"
            )
        return _shortcut_label(self.leader, key)

    def available(self) -> list[Shortcut]:
        return [
            shortcut
            for shortcut in self.shortcuts
            if shortcut.filter()
            and (self.leader or self.vi_leader or has_ctrl_chord(shortcut.key))
            and not (self.pending and self._pending_leader == (shortcut.key,))
        ]

    def summary(self) -> str:
        """The single help affordance, never a list of individual bindings."""
        leader = self.leader
        if self.pending:
            leader = self._pending_leader or leader
        elif self.vi_leader and self.vi_leader_enabled():
            leader = self.vi_leader
        key = _leader_label(leader) if leader else HELP_LABEL
        return f"{compact_label(key)} …" if self.pending else f"{key} Keys"

    def hint_rows(self) -> list[tuple[str, str]]:
        """The which-key list: (key, what it does). ``hint_footer`` says how to leave."""
        if self.choices:
            return [(choice.key, choice.label) for choice in self.choices]
        actions = [
            # A group with one member showing reads as that shortcut alone.
            (
                self._keys_label([shortcut.key for shortcut in run]),
                run[0].group if len(run) > 1 else run[0].label,
            )
            for run in self._grouped(self.available())
        ]
        return self.help_provider() + actions if self.browsing or self._vi_pending else actions

    @property
    def _vi_pending(self) -> bool:
        """The vi alias is waiting: its menu doubles as the full help view."""
        return bool(self.vi_leader) and self.pending and self._pending_leader == self.vi_leader

    def hint_footer(self) -> list[tuple[str, str]]:
        """How to back out of the open overlay, for its bottom border."""
        if self.browsing:
            return [(f"Esc / {HELP_LABEL}", "close")]
        if self.choices or self._vi_pending:
            return [("Esc", "cancel")]
        return [("Esc", "cancel"), (HELP_LABEL, "all keys")]

    @staticmethod
    def _grouped(shortcuts: list[Shortcut]) -> list[list[Shortcut]]:
        runs: list[list[Shortcut]] = []
        for shortcut in shortcuts:
            if runs and shortcut.group is not None and runs[-1][-1].group == shortcut.group:
                runs[-1].append(shortcut)
            else:
                runs.append([shortcut])
        return runs

    def _keys_label(self, keys: list[str]) -> str:
        """``n / p`` while the leader waits, else ``Ctrl+B n / p`` or ``Ctrl+N / Ctrl+P``."""
        if self.pending:
            return " / ".join(keys)
        if self.leader:
            return f"{self.leader_label} {' / '.join(keys)}"
        return " / ".join(self.label(key) for key in keys)
