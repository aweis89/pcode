"""Browse and edit saved defaults without leaving the configuration popup."""

import json
from collections.abc import Callable

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import DynamicContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import TextArea
from rich.text import Text

from pcode import config
from pcode.frame import Frame
from pcode.popup_ui import RichPane, focus_overlay, fuzzy_match, popup_container, popup_style
from pcode.preferences import (
    SETTINGS,
    USER_ONLY,
    project_preferences_path,
    read_preferences,
    valid_preferences,
)
from pcode.prefix_keys import PrefixKeys


def _display(value) -> str:
    return "unset" if value is None else json.dumps(value, ensure_ascii=False)


class ConfigBrowser:
    def __init__(self, *, save: Callable[[list[str]], str], input=None, output=None, style=None):
        self.save = save
        self.scope = "user"
        self.changed_only = False
        self.selected = 0
        self.editing = False
        self.choices: tuple[str, ...] = ()
        self.choice = 0
        self.message = ""
        self.search = TextArea(height=1, prompt="Filter: ", multiline=False)
        self.value = TextArea(height=1, prompt="Value: ", multiline=False)
        self.search.buffer.on_text_changed += self.filter
        self.reload()
        self.filter()
        browsing = Condition(lambda: not self.editing)
        editing_text = Condition(lambda: self.editing and not self.choices)
        rows = FormattedTextControl(
            self.fragments, get_cursor_position=lambda: Point(x=0, y=self.selected)
        )
        self.rows = Window(
            rows,
            wrap_lines=False,
            always_hide_cursor=True,
        )
        self.choice_rows = Window(
            FormattedTextControl(
                self.choice_fragments,
                focusable=True,
                get_cursor_position=lambda: Point(x=0, y=self.choice),
            ),
            always_hide_cursor=True,
        )
        self.detail = RichPane()
        self._detail_text = None
        keys = KeyBindings()
        self.detail.bind_scrolling(keys)
        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)
        selecting = ~editing_text & ~has_focus(self.detail.window)
        shortcuts = self.shortcuts = PrefixKeys()
        shortcuts.set_help(
            lambda: [
                ("Type", "Filter settings, or edit a value"),
                ("↑/↓ / Ctrl+P/N", "Select setting or enum value"),
                ("PgUp/PgDn / Ctrl+U/D", "Page / half page settings or details"),
                ("Tab/Shift+Tab", "Switch between settings/editor and details"),
                ("Enter", "Edit setting / save value"),
                ("Esc / Ctrl+C / Ctrl+L", "Cancel edit / close browser"),
            ]
        )

        @keys.add("up", eager=True, filter=selecting)
        @keys.add("c-p", eager=True, filter=selecting)
        def previous(event):
            self.move(-1)

        @keys.add("down", eager=True, filter=selecting)
        @keys.add("c-n", eager=True, filter=selecting)
        def next_row(event):
            self.move(1)

        for key, direction, half in (
            ("pageup", -1, False),
            ("pagedown", 1, False),
            ("c-u", -1, True),
            ("c-d", 1, True),
        ):

            def page(event, direction=direction, half=half):
                info = self.rows.render_info
                size = max(1, info.window_height - 1) if info else 10
                self.move(direction * (max(1, (size + 1) // 2) if half else size))

            keys.add(key, eager=True, filter=browsing & selecting)(page)

        @keys.add("enter", eager=True)
        def accept(event):
            if self.editing:
                value = self.choices[self.choice] if self.choices else self.value.text
                self.apply("set", value)
            elif self.matches:
                self.begin_edit()

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        @keys.add("c-l")
        def cancel(event):
            if self.editing:
                self.end_edit()
                self.message = "Edit cancelled; nothing saved."
            else:
                event.app.exit()

        @shortcuts.add(
            "t",
            "Toggle user/project scope",
            filter=browsing & Condition(lambda: self.project_path is not None),
        )
        def toggle_scope(event):
            self.scope = "project" if self.scope == "user" else "user"
            self.message = ""
            self.filter()

        @shortcuts.add("o", "Toggle overridden settings only", filter=browsing)
        def toggle_changed(event):
            self.changed_only = not self.changed_only
            self.message = ""
            self.filter()

        @shortcuts.add(
            "r",
            "Remove selected scope override",
            filter=browsing & Condition(lambda: bool(self.matches)),
        )
        def reset(event):
            self.apply("unset")

        def line(text):
            return Window(FormattedTextControl(text), height=1, wrap_lines=False)

        # Only terminal dimensions determine the zones, never a setting's text,
        # result count, or edit state. A bare Frame fills the screen without the
        # content-sized Box and Shadow used by a small Dialog.
        editor = HSplit([self.value, Window()])
        selection = HSplit(
            [
                DynamicContainer(
                    lambda: (
                        self.choice_rows if self.choices else editor if self.editing else self.rows
                    )
                )
            ],
            height=lambda: max(1, (get_app().output.get_size().rows - 9) // 3),
        )
        filter_label = line(lambda: f"Filter: {self.search.text}")
        self.detail.window.height = Dimension(min=1)
        frame = Frame(
            title="Saved configuration",
            body=HSplit(
                [
                    line(
                        lambda: (
                            f"Scope: {self.scope}"
                            + (
                                " · no project workspace"
                                if self.project_path is None
                                else f" · {shortcuts.label('t')} Switch scope"
                            )
                        )
                    ),
                    line(
                        lambda: (
                            f"Show: {'Overrides only' if self.changed_only else 'All settings'}"
                            f" · {shortcuts.label('o')} Toggle"
                            f" · {shortcuts.label('r')} Reset override"
                        )
                    ),
                    DynamicContainer(lambda: filter_label if self.editing else self.search),
                    selection,
                    self.detail,
                    Window(FormattedTextControl(lambda: self.message), height=2, wrap_lines=True),
                    line("↑/↓ Select · Enter Edit / Save · Esc Cancel / Close"),
                    line(shortcuts.summary),
                ],
            ),
        )
        self.app = Application(
            layout=Layout(popup_container(frame, shortcuts), focused_element=self.search),
            key_bindings=shortcuts.key_bindings(keys),
            full_screen=True,
            input=input,
            output=output,
            style=popup_style(style),
            before_render=self.refresh_details,
        )

    def refresh_details(self, app) -> None:
        text = self.details()
        if text != self._detail_text:
            self._detail_text = text
            self.detail.set([Text(text)])

    def reload(self) -> None:
        self.project_path = project_preferences_path()
        self.user = read_preferences()
        self.project = read_preferences(self.project_path) if self.project_path is not None else {}
        self.valid_project = {
            k: v for k, v in valid_preferences(self.project).items() if k not in USER_ONLY
        }
        self.data = valid_preferences(self.user) | self.valid_project

    def filter(self, buffer=None) -> None:
        terms = self.search.text.casefold().split()
        self.matches = [
            key
            for key in config.listed_settings()
            if (self.scope == "user" or key not in USER_ONLY)
            and (
                not self.changed_only
                or key in (self.project if self.scope == "project" else self.user)
            )
            and all(
                fuzzy_match(term, f"{key} {SETTINGS[key].description}".casefold()) for term in terms
            )
        ]
        self.selected = 0

    def effective(self, key: str) -> tuple[str | None, str]:
        value = config._effective(self.data, key)
        source = (
            "project" if key in self.valid_project else "user" if key in self.data else "default"
        )
        return value, source

    def fragments(self):
        if not self.matches:
            return [("class:plan", "No matching settings.")]
        result = []
        for index, key in enumerate(self.matches):
            value, source = self.effective(key)
            result.append(
                (
                    "class:selected" if index == self.selected else "",
                    f"{'›' if index == self.selected else ' '} {key} = {_display(value)}"
                    f" · {source}",
                )
            )
            if index < len(self.matches) - 1:
                result.append(("", "\n"))
        return result

    def details(self) -> str:
        if not self.matches:
            return "Saved effective values include user and project overrides."
        key = self.matches[self.selected]
        setting = SETTINGS[key]
        scope = self.project if self.scope == "project" else self.user
        value, source = self.effective(key)
        override = _display(scope[key]) if key in scope else "none (inherited)"
        timing = (
            "Layout setting; applies immediately when saved"
            if key in config.IMMEDIATE_SETTINGS
            else "Saved default; may require next launch"
        )
        suggestions = (
            f"\nSuggestions: {', '.join(setting.choices)}"
            if setting.choices and (setting.positive_integer or setting.whole_number)
            else ""
        )
        shadowed = (
            "\nProject override takes precedence over edits to the user value."
            if self.scope == "user" and source == "project"
            else ""
        )
        return (
            f"{key}: {setting.description}\nDefault: {_display(setting.default)}"
            f" · {self.scope} override: {override}\n"
            f"Saved effective: {_display(value)} (from {source})\n{timing}{suggestions}{shadowed}"
        )

    def choice_fragments(self):
        result = []
        for index, value in enumerate(self.choices):
            if index:
                result.append(("", "\n"))
            result.append(
                (
                    "class:selected" if index == self.choice else "",
                    f"{'›' if index == self.choice else ' '} {value}",
                )
            )
        return result

    def move(self, amount: int) -> None:
        if self.editing:
            self.choice = max(0, min(len(self.choices) - 1, self.choice + amount))
        else:
            self.selected = max(0, min(len(self.matches) - 1, self.selected + amount))

    def begin_edit(self) -> None:
        key = self.matches[self.selected]
        setting = SETTINGS[key]
        scope = self.project if self.scope == "project" else self.user
        current = scope.get(key, self.effective(key)[0])
        self.editing = True
        self.message = "Enter saves; Escape cancels."
        self.choices = (
            setting.choices if not (setting.positive_integer or setting.whole_number) else ()
        )
        self.choice = self.choices.index(current) if current in self.choices else 0
        self.value.text = current if isinstance(current, str) else ""
        self.value.buffer.cursor_position = len(self.value.text)
        focus_overlay(self.app, self.choice_rows if self.choices else self.value)

    def end_edit(self) -> None:
        self.editing = False
        self.choices = ()
        focus_overlay(self.app, self.search)

    def apply(self, action: str, value: str | None = None) -> None:
        key = self.matches[self.selected]
        args = ["project"] if self.scope == "project" else []
        args.extend([action, key])
        try:
            if action == "set":
                assert value is not None
                SETTINGS[key].validate(key, value)
                args.append(value)
            self.message = self.save(args)
            self.reload()
        except (ValueError, OSError) as error:
            self.message = str(error)
            return
        if self.editing:
            self.end_edit()
        # Removing an override can remove this row from the filtered list.
        # Keep the same setting selected when it still belongs in the results.
        self.filter()
        if key in self.matches:
            self.selected = self.matches.index(key)

    async def run(self):
        return await self.app.run_async()
