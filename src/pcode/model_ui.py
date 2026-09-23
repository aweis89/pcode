"""Searchable, temporary model picker; never owns the main editor's buffer."""

import re

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Dialog, Label, TextArea

from pcode.models import PROVIDERS
from pcode.popup_ui import fuzzy_match, popup_container, popup_style


def matches_model(term: str, model: str) -> bool:
    """Match the id or the provider's display name plus model, e.g. anth + opus."""
    provider, _, name = model.partition(":")
    return any(
        fuzzy_match(term, text)
        for text in (model.casefold(), f"{PROVIDERS.get(provider, provider)} {name}".casefold())
    )


class ModelPicker:
    def __init__(self, models, providers, *, current=None, input=None, output=None, style=None):
        self.models = list(models)
        self.providers = set(providers)
        self.current = current
        self.selected = 0
        self.matches = list(models)
        self.search = TextArea(height=1, prompt="Filter: ", multiline=False)
        self.search.buffer.on_text_changed += self.filter
        rows = FormattedTextControl(
            self.fragments, get_cursor_position=lambda: Point(x=0, y=self.selected)
        )
        self.rows = Window(
            rows,
            height=Dimension(min=3, max=14),
            dont_extend_height=True,
            wrap_lines=False,
            always_hide_cursor=True,
        )
        keys = KeyBindings()

        @keys.add("up", eager=True)
        @keys.add("c-p", eager=True)
        def previous(event):
            self.move(-1)

        @keys.add("down", eager=True)
        @keys.add("c-n", eager=True)
        def next_model(event):
            self.move(1)

        # The same paging keys as every other popup, moving the selection.
        @keys.add("pageup", eager=True)
        def previous_page(event):
            self.move(-self.page())

        @keys.add("pagedown", eager=True)
        def next_page(event):
            self.move(self.page())

        @keys.add("c-u", eager=True)
        def previous_half(event):
            self.move(-max(1, (self.page() + 1) // 2))

        @keys.add("c-d", eager=True)
        def next_half(event):
            self.move(max(1, (self.page() + 1) // 2))

        @keys.add("enter", eager=True)
        def accept(event):
            if self.matches:
                event.app.exit(result=self.matches[self.selected])

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        @keys.add("c-l")
        def cancel(event):
            event.app.exit(result=None)

        dialog = Dialog(
            title="Choose model",
            body=HSplit(
                [
                    Label(
                        "↑↓ Select · PgUp/PgDn Page · Ctrl+U/D Half page · "
                        "Enter apply · Esc cancel",
                        dont_extend_height=True,
                    ),
                    self.search,
                    self.rows,
                    Label(
                        "Local suggestions; access depends on your account.\n"
                        "Type provider:model-id for a custom model.\n"
                        "Changing model continues the current conversation.",
                        dont_extend_height=True,
                    ),
                ],
                padding=1,
            ),
            with_background=True,
        )
        self.app = Application(
            layout=Layout(popup_container(dialog), focused_element=self.search),
            key_bindings=keys,
            full_screen=True,
            input=input,
            output=output,
            style=popup_style(style),
        )

    def move(self, rows: int) -> None:
        self.selected = max(0, min(len(self.matches) - 1, self.selected + rows))

    def page(self) -> int:
        """Rows actually visible, falling back before the first render."""
        info = self.rows.render_info
        return max(1, info.window_height - 1) if info is not None else 10

    def filter(self, buffer):
        query = buffer.text.strip()
        terms = query.casefold().split()
        self.matches = [name for name in self.models if all(matches_model(t, name) for t in terms)]
        provider, _, name = query.partition(":")
        # Any supported provider, not only configured ones: the key may be
        # exported after launch, and infer_model reports a missing key clearly.
        if (
            provider in PROVIDERS
            and name
            and re.fullmatch(r"[A-Za-z0-9_.:/-]+", name)
            and query not in self.matches
        ):
            self.matches.insert(0, query)
        self.selected = 0

    def fragments(self):
        if not self.matches:
            return [("class:plan", "No matches. Try another filter or provider:model-id.")]
        result = []
        for index, name in enumerate(self.matches):
            provider, _, model = name.partition(":")
            marker = " · current" if name == self.current else ""
            if name not in self.models:
                marker += " · custom"
            style = "class:selected" if index == self.selected else ""
            result.append(
                (
                    style,
                    f"{'›' if index == self.selected else ' '} "
                    f"{PROVIDERS.get(provider, provider)} · {model}{marker}",
                )
            )
            if index < len(self.matches) - 1:
                result.append(("", "\n"))
        return result

    async def run(self):
        return await self.app.run_async()
