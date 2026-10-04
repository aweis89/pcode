"""Copy a response, or one quote or code block out of it, to the clipboard.

The transcript draws a Markdown quote with a `▌` rail and hard-wraps it to the
terminal, so selecting it with the mouse picks up both. The source still has
the text as written: this pulls each top-level quote and fenced code block out
of it for a small picker, shared by `/copy` and `/tree`.
"""

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt
from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Label, TextArea

from pcode.frame import Dialog
from pcode.popup_ui import bind_list_paging, popup_container, popup_mouse, popup_style
from pcode.prefix_keys import PrefixKeys

_QUOTE_MARKER = re.compile(r"^[ \t]*> ?")
_ROW_WIDTH = 80


@dataclass(frozen=True)
class Snippet:
    kind: str  # "response", "quote", or "code"
    text: str
    language: str = ""

    @property
    def label(self) -> str:
        if self.kind == "response":
            return "Whole response"
        if self.language:
            return f"{self.kind.title()} ({self.language})"
        return self.kind.title()


def _unquote(lines: list[str]) -> str:
    # A lazy continuation line has no marker; it only loses its indentation.
    return "\n".join(
        _QUOTE_MARKER.sub("", line, count=1) if _QUOTE_MARKER.match(line) else line.lstrip()
        for line in lines
    ).strip("\n")


def snippets(markdown: str) -> list[Snippet]:
    """The whole response, then each top-level quote and code block in order."""
    if not markdown.strip():
        return []
    found = [Snippet("response", markdown)]
    lines = markdown.splitlines()
    depth = 0
    for token in MarkdownIt("commonmark").parse(markdown):
        if token.type == "blockquote_open":
            if depth == 0 and token.map:
                start, end = token.map
                if text := _unquote(lines[start:end]):
                    found.append(Snippet("quote", text))
            depth += 1
        elif token.type == "blockquote_close":
            depth -= 1
        elif token.type == "fence" and depth == 0 and token.content.strip():
            language = token.info.split()[0] if token.info.strip() else ""
            # No trailing newline: pasted into a shell, it would run the command.
            text = token.content.rstrip("\n")
            found.append(Snippet("code", text, language))
    return found


def last_response(tree) -> str:
    """The newest assistant text on the active branch, skipping compactions."""
    for identity in reversed(tree.path(tree.active)):
        node = tree.nodes[identity]
        if node.kind != "compaction" and node.response.strip():
            return node.response
    return ""


def snippet_row(snippet: Snippet) -> str:
    text = " ".join(snippet.text.split())
    row = f"{snippet.label}: {text}"
    return row if len(row) <= _ROW_WIDTH else row[: _ROW_WIDTH - 1] + "…"


class SnippetPicker:
    """A list of snippets; Enter picks one, Esc backs out.

    ``container`` and ``key_bindings`` let a popup host it as an overlay, which
    `/tree` does; ``snippet_dialog`` wraps it in an application of its own.
    """

    def __init__(self, choices: list[Snippet], on_pick, on_cancel, *, shortcuts=None) -> None:
        self.choices = choices
        self.on_pick = on_pick
        self.list = TextArea(
            read_only=True,
            wrap_lines=False,
            scrollbar=True,
            width=Dimension(min=40, preferred=_ROW_WIDTH + 1),
            height=Dimension(min=1, preferred=len(choices)),
        )
        self.list.window.cursorline = Always()
        rows = [snippet_row(snippet) for snippet in choices]
        # Start on the first quote or block: it is why the picker opened.
        start = 1 if len(choices) > 1 else 0
        position = sum(len(row) + 1 for row in rows[:start])
        self.list.buffer.set_document(Document("\n".join(rows), position), bypass_readonly=True)
        keys = self.key_bindings = KeyBindings()
        bind_list_paging(keys, self.list, Always())

        @keys.add("enter", eager=True)
        def pick(event):
            on_pick(self.selected())

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        def cancel(event):
            on_cancel()

        self.container = HSplit(
            [self.list],
            # Gated by the host's `PrefixKeys`, so a waiting leader owns Enter and Esc.
            key_bindings=shortcuts.gate(keys) if shortcuts else keys,
            modal=True,
        )

    def help(self) -> list[tuple[str, str]]:
        return [
            ("↑/↓", "Select snippet"),
            ("PgUp/PgDn", "Page"),
            ("Ctrl+U/D", "Half page"),
            ("Enter", "Copy selected snippet"),
            ("Esc / Ctrl+C", "Cancel"),
        ]

    def selected(self) -> Snippet:
        row = self.list.document.cursor_position_row
        return self.choices[min(row, len(self.choices) - 1)]


def snippet_dialog(choices: list[Snippet], *, input=None, output=None, style=None):
    """A popup application whose result is the picked snippet, or None."""
    app: Application
    shortcuts = PrefixKeys()

    picker = SnippetPicker(
        choices,
        on_pick=lambda snippet: app.exit(result=snippet),
        on_cancel=lambda: app.exit(result=None),
        shortcuts=shortcuts,
    )
    shortcuts.set_help(picker.help)
    dialog = Dialog(
        title="Copy",
        body=HSplit([picker.container, Label(shortcuts.summary)], padding=1),
    )
    app = Application(
        layout=Layout(popup_container(dialog, shortcuts), focused_element=picker.list),
        key_bindings=shortcuts.key_bindings(KeyBindings()),
        full_screen=True,
        mouse_support=popup_mouse(shortcuts),
        input=input,
        output=output,
        style=popup_style(style),
    )
    return app
