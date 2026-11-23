"""Masked single-line secret prompt; never touches the main editor, history, or drafts."""

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.widgets import Label, TextArea

from pcode.frame import Dialog
from pcode.popup_hints import popup_bar
from pcode.popup_ui import popup_container, popup_style
from pcode.prefix_keys import PrefixKeys


class SecretPrompt:
    def __init__(
        self,
        title: str,
        prompt: str = "Secret: ",
        *,
        detail: str = "",
        input=None,
        output=None,
        style=None,
    ) -> None:
        self.field = TextArea(height=1, prompt=prompt, multiline=False, password=True)
        keys = KeyBindings()
        shortcuts = self.shortcuts = PrefixKeys()
        shortcuts.set_help(
            lambda: [
                ("Paste / Type", "Enter value (masked; never saved to history or drafts)"),
                ("Enter", "Submit"),
                ("Esc / Ctrl+C", "Cancel"),
            ]
        )

        @keys.add("enter", eager=True)
        def accept(event):
            if self.field.text.strip():
                event.app.exit(result=self.field.text)

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        def cancel(event):
            event.app.exit(result=None)

        body = []
        if detail:
            body.append(Label(detail, dont_extend_height=True))
        body.append(self.field)
        dialog = Dialog(
            title=title,
            body=HSplit([HSplit(body, padding=1), popup_bar(shortcuts, "secret", gap=True)]),
        )
        self.app = Application(
            layout=Layout(popup_container(dialog, shortcuts), focused_element=self.field),
            key_bindings=shortcuts.key_bindings(keys),
            full_screen=True,
            input=input,
            output=output,
            style=popup_style(style),
        )

    async def run(self) -> str | None:
        return await self.app.run_async()
