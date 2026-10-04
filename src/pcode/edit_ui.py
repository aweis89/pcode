"""Temporary alternate-screen diff browser, separate from inline scrollback."""

import asyncio
import dataclasses
from collections.abc import Callable, Sequence

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.widgets import Label, TextArea

from pcode.delta import Delta
from pcode.edit_transcript import DiffLexer, text_fragments
from pcode.edits import edit_text
from pcode.frame import Frame
from pcode.git_diff import DiffView
from pcode.popup_ui import (
    bind_list_paging,
    fuzzy_match,
    list_pane_height,
    popup_container,
    popup_mouse,
    popup_style,
    steer_list_from_query,
)
from pcode.prefix_keys import PrefixKeys
from pcode.runtime import EditCompleted

EMPTY = "No file edits in this conversation."
NO_MATCH = "No matching edits."
PROMPTS = {"paths": "Search paths: ", "diffs": "Search diff lines: "}
LOADING = DiffView("Loading…", [], "Loading…")


def first_view(
    loaders: Sequence[Callable[[], DiffView]],
) -> tuple[int, dict[int, DiffView]]:
    """Load views in order until one has changes and open on it, else on the first.

    Returns the index to open on and every view loaded on the way.
    """
    loaded = {}
    for index, load in enumerate(loaders):
        loaded[index] = view = load()
        if view.changes:
            if index:
                loaded[index] = dataclasses.replace(
                    view, title=f"{view.title} · opened here: earlier views are empty"
                )
            return index, loaded
    return 0, loaded


def change_title(change: EditCompleted) -> str:
    return (
        f"{change.operation:9} {edit_text(change.path)} · +{change.added} −{change.removed}"
    ).replace("\n", " ↵ ")


def change_heading(change: EditCompleted) -> str:
    return (
        f"{change.operation.capitalize()} {edit_text(change.path)}"
        f" · +{change.added} −{change.removed}"
    )


def change_diff(change: EditCompleted, patch: str | None = None) -> str:
    """Render one completed change exactly as the scrollback block describes it.

    `patch` replaces the change's own patch text, as delta's rendering of it.
    """
    lines = [change_heading(change), ""]
    if change.patch:
        lines.append(edit_text(change.patch) if patch is None else patch)
        if change.truncated:
            lines.append("… additional diff rows omitted")
    if change.omitted:
        lines.append(f"Diff unavailable: {edit_text(change.omitted)}")
    return "\n".join(lines)


def delta_diff(
    change: EditCompleted, delta: Delta | None, width: int
) -> tuple[str, dict[int, list[tuple[str, str]]]] | None:
    """The change as delta renders it: plain text, and the styled rows by number.

    None when there is no delta or no patch, or delta failed.
    """
    if delta is None or not change.patch:
        return None
    lines = delta.render(edit_text(change.patch), width)
    if lines is None:
        return None
    # The patch starts after the heading and its blank row.
    styled = [text_fragments(line) for line in lines]
    patch = "\n".join("".join(text for _, text in row) for row in styled)
    # The patch follows the heading, however many rows it takes, and a blank row.
    start = change_heading(change).count("\n") + 2
    return change_diff(change, patch), {start + n: row for n, row in enumerate(styled)}


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

    One search line serves both panes. The ``f`` shortcut in the file list
    searches paths and filters the list; in the diff it searches diff lines,
    filters the list to changes with a match, and jumps the diff to the first.
    Changes are listed in the order given; `title` says what they are.

    Given `views`, the ``v`` shortcut cycles through them: each loader runs
    off the event loop the first time it is shown and is kept for the rest of
    the popup. `loaded` holds views already loaded, including `view`, the one
    to open on. A switch keeps the search and, where the new view has it, the
    selected file.
    """

    def __init__(
        self,
        changes=(),
        *,
        title: str = "Edit diffs",
        empty: str = EMPTY,
        views: Sequence[Callable[[], DiffView]] = (),
        loaded: dict[int, DiffView] | None = None,
        view: int = 0,
        code_theme: str = "monokai",
        delta: Delta | None = None,
        key_prefix: str | None = None,
        **app_options,
    ) -> None:
        self.delta = delta
        self.delta_width = 0
        self.prefetched: tuple[int, int] | None = None
        if not views:
            opening = DiffView(title, list(changes), empty)
            views, loaded, view = [lambda: opening], {0: opening}, 0
        self.views = list(views)
        self.loaded = dict(loaded or {})
        self.view = view
        self.pending: set[int] = set()
        self.keep: str | None = None
        if view not in self.loaded:
            self.loaded[view] = self.views[view]()
        current = self.loaded[view]
        self.title = current.title
        self.changes = list(current.changes)
        self.empty = current.empty
        self.visible: list[EditCompleted] = []
        self.selected: EditCompleted | None = None
        self.scope = "paths"
        self._refreshing = False
        self.query = TextArea(height=1, prompt=lambda: PROMPTS[self.scope], multiline=False)
        self.files = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        self.files.window.cursorline = Always()
        self.lexer = DiffLexer(code_theme)
        self.diff = TextArea(read_only=True, wrap_lines=True, scrollbar=True, lexer=self.lexer)
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

        @keys.add("enter", filter=has_focus(self.query))
        def search_done(event):
            event.app.layout.focus(self.files if self.scope == "paths" else self.diff)

        self.prefix_keys = shortcuts = PrefixKeys(key_prefix)
        shortcuts.set_help(
            lambda: [
                ("↑/↓", "Select file / scroll diff"),
                ("PgUp/PgDn", "Page"),
                ("Ctrl+U/D", "Half page"),
                ("Ctrl+Home/End", "First / last row"),
                ("Type", "Search in the search field"),
                ("Enter", "Leave the search field"),
                ("Tab/Shift+Tab", "Switch files / diff"),
                ("Esc/Ctrl+C", "Close"),
            ]
        )

        @shortcuts.add("f", "Search the focused pane")
        def search(event):
            # From the search line itself, keep searching what it searches.
            if event.app.layout.has_focus(self.files):
                self.search("paths")
            elif event.app.layout.has_focus(self.diff):
                self.search("diffs")
            else:
                self.search(self.scope)

        # Emacs's incremental search keys: s forward, r in reverse.
        @shortcuts.add("s", "Next match", group="Next / previous match")
        def next_match(event):
            self.jump(1)

        @shortcuts.add("r", "Previous match", group="Next / previous match")
        def previous_match(event):
            self.jump(-1)

        @shortcuts.add("v", "Next view", filter=Condition(lambda: len(self.views) > 1))
        def next_view(event):
            self.show_view((self.view + 1) % len(self.views))

        header = Label(
            lambda: (
                f"{self.position()}/{len(self.visible)} · "
                f"{edit_text(self.selected.path) if self.selected else 'none'}"
            )
        )
        root = HSplit(
            [
                Label(self.heading),
                header,
                self.query,
                Frame(self.diff, title="Diff"),
                Frame(
                    self.files,
                    title="Files",
                    height=lambda: list_pane_height(len(self.changes)),
                ),
                Label(shortcuts.summary),
            ]
        )
        self.app = Application(
            # Open in the search line, so typing filters straight away.
            layout=Layout(popup_container(root, shortcuts), focused_element=self.query),
            key_bindings=shortcuts.key_bindings(keys),
            full_screen=True,
            mouse_support=popup_mouse(shortcuts),
            style=popup_style(app_options.pop("style", None)),
            **app_options,
        )
        self.app.before_render += self.rewidth
        self.refresh()

    def heading(self) -> str:
        if len(self.views) == 1:
            return self.title
        return f"View {self.view + 1}/{len(self.views)} · {self.title}"

    def show_view(self, index: int) -> None:
        # Remembered across a load, which shows nothing selected meanwhile.
        if self.selected is not None:
            self.keep = self.selected.path
        self.view = index
        if index in self.loaded:
            self.apply(self.loaded[index])
            return
        self.apply(LOADING)
        if index not in self.pending:
            self.pending.add(index)
            self.app.create_background_task(self.load(index))

    async def load(self, index: int) -> None:
        try:
            view = await asyncio.to_thread(self.views[index])
        except Exception as error:  # a broken loader must not take the popup down
            view = DiffView("View failed", [], f"Could not load this view: {error}")
        self.loaded[index] = view
        self.pending.discard(index)
        if self.view == index:
            self.apply(view)
            self.app.invalidate()

    def apply(self, view: DiffView) -> None:
        """Show `view`, staying on the selected file if the new view has it."""
        self.title, self.changes, self.empty = view.title, list(view.changes), view.empty
        self.selected = next((c for c in self.changes if c.path == self.keep), None)
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
        self.show_diff()
        self.diff.window.vertical_scroll = 0
        rows = self.diff_rows()
        if rows:
            self.go_to(rows[0])

    def pane_width(self) -> int:
        # The frame's two borders, the scrollbar, and a spare column: a wrapping
        # window pushes a row as wide as itself onto a blank one, and every
        # padded delta row is exactly the width it was rendered for.
        return max(1, self.app.output.get_size().columns - 4)

    def show_diff(self) -> None:
        """Put the selected change in the diff pane, through delta where it is on."""
        change = self.selected
        width = self.pane_width()
        if self.delta is not None and self.prefetched != (width, id(self.changes)):
            # One delta run for the whole view, rather than one per file selected.
            self.prefetched = (width, id(self.changes))
            patches = [edit_text(c.patch) for c in self.changes if c.patch]
            self.delta.prefetch(patches, width)
        self.delta_width = width
        rendered = delta_diff(change, self.delta, self.delta_width) if change else None
        if rendered is not None:
            text, self.lexer.rows = rendered
        else:
            text = change_diff(change) if change else NO_MATCH if self.changes else self.empty
            self.lexer.rows = {}
        self.diff.buffer.set_document(Document(text, 0), bypass_readonly=True)

    def rewidth(self, _app) -> None:
        """delta lays a diff out for one width, so a resize renders it again."""
        if self.delta is None or self.selected is None:
            return
        if self.pane_width() == self.delta_width:
            return
        row = self.diff.document.cursor_position_row
        self.show_diff()
        self.go_to(min(row, self.diff.document.line_count - 1))

    async def run(self) -> None:
        await self.app.run_async()
