"""Temporary alternate-screen diff browser, separate from inline scrollback."""

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.widgets import Frame, Label, TextArea

from pcode.edit_transcript import DiffLexer
from pcode.edits import edit_text
from pcode.popup_ui import (
    bind_list_paging,
    fuzzy_match,
    list_pane_height,
    popup_container,
    popup_mouse,
    popup_style,
    steer_list_from_query,
)
from pcode.runtime import EditCompleted

EMPTY = "No file edits in this conversation."
NO_MATCH = "No matching edits."
KEYS = (
    "↑↓ Select/scroll · PgUp/PgDn Page · Ctrl+U/D Half page · Ctrl+Home/End First/last · "
    "Type to search paths, Enter to leave · Tab Focus · "
    "/ Search paths (in Files) or diff lines (in Diff) · n/N Next/previous match · Esc Close"
)
PROMPTS = {"paths": "Search paths: ", "diffs": "Search diff lines: "}


def change_title(change: EditCompleted) -> str:
    return (
        f"{change.operation:9} {edit_text(change.path)} · +{change.added} −{change.removed}"
    ).replace("\n", " ↵ ")


def change_diff(change: EditCompleted) -> str:
    """Render one completed change exactly as the scrollback block describes it."""
    lines = [
        f"{change.operation.capitalize()} {edit_text(change.path)}"
        f" · +{change.added} −{change.removed}",
        "",
    ]
    if change.patch:
        lines.append(edit_text(change.patch))
        if change.truncated:
            lines.append("… additional diff rows omitted")
    if change.omitted:
        lines.append(f"Diff unavailable: {edit_text(change.omitted)}")
    return "\n".join(lines)


def matching_rows(text: str, terms: list[str]) -> list[int]:
    """Rows where every query word fuzzy-matches the line, in document order."""
    if not terms:
        return []
    return [
        row
        for row, line in enumerate(text.casefold().splitlines())
        if all(fuzzy_match(term, line) for term in terms)
    ]


class EditBrowser:
    """Most of the screen is the diff; the file selector stays a small bottom pane.

    One search line serves both panes. Pressing ``/`` in the file list searches
    paths and filters the list; pressing it in the diff searches diff lines,
    filters the list to changes with a match, and jumps the diff to the first.
    Changes are listed in the order given; `title` says what they are.
    """

    def __init__(
        self,
        changes,
        *,
        title: str = "Edit diffs",
        empty: str = EMPTY,
        code_theme: str = "monokai",
        **app_options,
    ) -> None:
        self.changes = list(changes)
        self.empty = empty
        self.visible: list[EditCompleted] = []
        self.selected: EditCompleted | None = None
        self.scope = "paths"
        self._refreshing = False
        self.query = TextArea(height=1, prompt=lambda: PROMPTS[self.scope], multiline=False)
        self.files = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        self.files.window.cursorline = Always()
        self.diff = TextArea(
            read_only=True,
            wrap_lines=True,
            scrollbar=True,
            lexer=DiffLexer(code_theme),
        )
        self.query.buffer.on_text_changed += lambda _: self.refresh()
        self.files.buffer.on_cursor_position_changed += lambda _: self.select()
        keys = KeyBindings()

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        def close(event):
            event.app.exit()

        # Tab only toggles the panes; the query line is entered with / and left with Enter.
        # The browser opens in it, searching paths.
        @keys.add("tab")
        @keys.add("s-tab")
        def toggle(event):
            focused = event.app.layout.has_focus(self.diff)
            event.app.layout.focus(self.files if focused else self.diff)

        # Paging keys act on the focused pane, as in every other popup.
        steer_list_from_query(keys, self.query, self.files)
        bind_list_paging(keys, self.files, has_focus(self.files) | has_focus(self.query))
        bind_list_paging(keys, self.diff, has_focus(self.diff))

        @keys.add("/", filter=has_focus(self.files))
        @keys.add("c-f", filter=has_focus(self.files))
        def search_paths(event):
            self.search("paths")

        @keys.add("/", filter=has_focus(self.diff))
        @keys.add("c-f", filter=has_focus(self.diff))
        def search_diffs(event):
            self.search("diffs")

        @keys.add("enter", filter=has_focus(self.query))
        def search_done(event):
            event.app.layout.focus(self.files if self.scope == "paths" else self.diff)

        @keys.add("n", filter=has_focus(self.files) | has_focus(self.diff))
        def next_match(event):
            self.jump(1)

        @keys.add("N", filter=has_focus(self.files) | has_focus(self.diff))
        def previous_match(event):
            self.jump(-1)

        header = Label(
            lambda: (
                f"{self.position()}/{len(self.visible)} · "
                f"{edit_text(self.selected.path) if self.selected else 'none'}"
            )
        )
        root = HSplit(
            [
                Label(title),
                header,
                Label(KEYS),
                self.query,
                Frame(self.diff, title="Diff"),
                Frame(self.files, title="Files", height=list_pane_height(len(self.changes))),
            ]
        )
        self.app = Application(
            # Open in the search line, so typing filters rather than reaching
            # the panes' one-key shortcuts.
            layout=Layout(popup_container(root), focused_element=self.query),
            key_bindings=keys,
            full_screen=True,
            mouse_support=popup_mouse(),
            style=popup_style(app_options.pop("style", None)),
            **app_options,
        )
        self.refresh()

    def position(self) -> int:
        return self.files.document.cursor_position_row + 1 if self.visible else 0

    def go_to(self, row: int) -> None:
        self.diff.buffer.cursor_position = self.diff.document.translate_row_col_to_index(row, 0)

    def terms(self) -> list[str]:
        return self.query.text.casefold().split()

    def search(self, scope: str) -> None:
        """Retarget the query line; a query typed for the other scope is dropped."""
        if scope != self.scope:
            self.scope = scope
            self.query.text = ""
        self.app.layout.focus(self.query)

    def matches(self, change: EditCompleted) -> bool:
        terms = self.terms()
        if self.scope == "paths":
            return all(fuzzy_match(term, change.path.casefold()) for term in terms)
        # Match the rendered text so redaction cannot hide the row a query matched.
        return not terms or bool(matching_rows(change_diff(change), terms))

    def diff_rows(self) -> list[int]:
        """Diff-pane rows matching a diff search; the path search never highlights rows."""
        if self.scope != "paths":
            return matching_rows(self.diff.text, self.terms())
        return []

    def jump(self, direction: int) -> None:
        """n/N move the diff cursor to the next or previous matching row, wrapping."""
        rows = self.diff_rows()
        if not rows:
            return
        current = self.diff.document.cursor_position_row
        if direction > 0:
            self.go_to(next((row for row in rows if row > current), rows[0]))
        else:
            self.go_to(next((row for row in reversed(rows) if row < current), rows[-1]))

    def refresh(self) -> None:
        previous = self.selected
        self.visible = [change for change in self.changes if self.matches(change)]
        selected = next((i for i, c in enumerate(self.visible) if c is previous), 0)
        lines = [change_title(change) for change in self.visible]
        text = "\n".join(lines) or (NO_MATCH if self.changes else self.empty)
        position = sum(len(line) + 1 for line in lines[:selected])
        self._refreshing = True
        self.files.buffer.set_document(Document(text, position), bypass_readonly=True)
        self._refreshing = False
        self.select(force=True)

    def select(self, force: bool = False) -> None:
        if self._refreshing:
            return
        row = self.files.document.cursor_position_row
        change = self.visible[row] if row < len(self.visible) else None
        if change is self.selected and change is not None and not force:
            return
        self.selected = change
        if change:
            text = change_diff(change)
        else:
            text = NO_MATCH if self.changes else self.empty
        self.diff.buffer.set_document(Document(text, 0), bypass_readonly=True)
        self.diff.window.vertical_scroll = 0
        rows = self.diff_rows()
        if rows:
            self.go_to(rows[0])

    async def run(self) -> None:
        await self.app.run_async()
