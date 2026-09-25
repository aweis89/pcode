"""Shared terminal-native styling for alternate-screen popups."""

import re
from collections.abc import Callable
from io import StringIO

from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition, Filter, has_focus
from prompt_toolkit.formatted_text import ANSI, AnyFormattedText, to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text, split_lines
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
from prompt_toolkit.styles import Style, merge_styles
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Console
from rich.theme import Theme

from pcode.input_keys import configure_newline_keys
from pcode.preferences import SETTINGS, load_preferences

# Rich writes Markdown links as OSC 8 hyperlinks on a terminal, but
# prompt_toolkit's ANSI parser reads CSI only and spills the rest as literal
# text ("8;id=1;https://…"). A pane cannot follow a link anyway, so drop them.
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Framed selector panes: rows of list content, before the frame's own borders.
LIST_ROWS_MIN = 3
LIST_ROWS_MAX = 6
_BORDERS = 2
# Rows a docked editor grows to before it scrolls; the panes above keep the rest.
INPUT_ROWS_MAX = 6
_INPUT_PROMPT = "\u203a "

_NATIVE = "bg:default fg:default noreverse"
POPUP_STYLE = Style.from_dict(
    {
        "popup": _NATIVE,
        "popup dialog": _NATIVE,
        "popup dialog.body": _NATIVE,
        "popup text-area": _NATIVE,
        "popup frame.border": _NATIVE,
        "popup frame.label": f"{_NATIVE} bold",
        "popup shadow": _NATIVE,
        "popup text-area last-line": "nounderline",
    }
)
# These are fallbacks for standalone popups without a caller theme. Keep them
# before the caller's style, unlike the terminal-native surfaces above.
POPUP_ACCENTS = Style.from_dict(
    {
        "popup scrollbar.background": _NATIVE,
        "popup scrollbar.button": f"{_NATIVE} reverse",
        "popup scrollbar.arrow": f"{_NATIVE} bold",
        "popup selected": f"{_NATIVE} reverse",
        "popup cursor-line": f"{_NATIVE} reverse nounderline",
        "popup placeholder": "dim italic",
    }
)


def popup_container(body):
    """Scope every modal surface under the same style class."""
    return HSplit([body], style="class:popup")


def popup_mouse() -> bool:
    """Whether popups capture the mouse, read as each one opens.

    Capturing gives clicks and wheel scrolling to the popup, but the terminal
    then stops treating a drag as a text selection.
    """
    return load_preferences().get("popup_mouse", SETTINGS["popup_mouse"].default) == "on"


def popup_style(base=None):
    """Let the caller theme highlights, but keep popup surfaces terminal-native."""
    styles = [POPUP_ACCENTS]
    if base is not None:
        styles.append(base)
    return merge_styles([*styles, POPUP_STYLE])


def _list_page(listing) -> int:
    info = listing.window.render_info
    return max(1, info.window_height - 1) if info else 10


def fuzzy_match(term: str, text: str) -> bool:
    """Match a casefolded substring or joined word prefixes, e.g. ed_ui + edit_ui.py.

    Gaps are allowed between words, not inside them. This keeps 'anthopus'
    from matching an unrelated Sonnet via scattered letters in 'anthropic:claude',
    and a three-letter query from matching every diff line with those initials.
    """
    if term in text:
        return True
    # Separators in the query are word boundaries, so ed_ui and ed/ui both find edit_ui.
    term = "".join(re.findall(r"[a-z0-9]+", term))
    if not term:
        return False
    # Track how much of the query can be consumed by successive word prefixes.
    positions = {0}
    for word in re.findall(r"[a-z0-9]+", text):
        following = set(positions)  # Skipping a word is allowed.
        for position in positions:
            for length, character in enumerate(word, 1):
                index = position + length - 1
                if index >= len(term) or character != term[index]:
                    break
                following.add(index + 1)
        if len(term) in following:
            return True
        positions = following
    return False


def bind_list_paging(keys, listing, filter) -> None:
    """Ctrl+U/Ctrl+D move a TextArea's cursor, a list's selection, by half a page.

    Every popup pane half-pages with these keys, so a browser feels the same
    whichever split has focus. ↑/↓ and PageUp/PageDown come from the TextArea
    itself (full-screen apps load prompt_toolkit's page navigation).
    """

    def half() -> int:
        return max(1, (_list_page(listing) + 1) // 2)

    @keys.add("c-u", filter=filter)
    def previous_half(event):
        listing.buffer.cursor_up(half())

    @keys.add("c-d", filter=filter)
    def following_half(event):
        listing.buffer.cursor_down(half())


def steer_list_from_query(keys, query, listing) -> None:
    """Keep typing in the query while ↑/↓ move the selection in a TextArea list.

    A one-line query swallows vertical movement otherwise, which is what a
    filter-as-you-type list makes people expect least.
    """
    typing = has_focus(query)

    def page() -> int:
        return _list_page(listing)

    @keys.add("up", filter=typing)
    @keys.add("c-p", filter=typing)
    def previous(event):
        listing.buffer.cursor_up()

    @keys.add("down", filter=typing)
    @keys.add("c-n", filter=typing)
    def following(event):
        listing.buffer.cursor_down()

    @keys.add("pageup", filter=typing)
    def previous_page(event):
        listing.buffer.cursor_up(page())

    @keys.add("pagedown", filter=typing)
    def following_page(event):
        listing.buffer.cursor_down(page())


class RichPane:
    """A read-only, scrollable pane showing Rich renderables (Markdown, Text).

    Rich output and prepared lines are cached until the content or width changes.
    Scrolling only creates a lightweight UIContent with the current cursor row;
    it must not rescan the entire transcript on every frame.
    """

    def __init__(
        self, *, theme: Theme | None = None, color_system: str | None = "truecolor"
    ) -> None:
        self.theme = theme
        self.color_system = color_system
        self.renderables: list = []
        self._version = 0
        self._cache: tuple[int, int, list, list[int]] | None = None
        self._line_cache: tuple[int, int, list] | None = None
        # Renderable index to scroll to on the next render, once the width is known.
        self._anchor: int | None = None
        pane = self

        class Control(UIControl):
            def is_focusable(self):
                return True

            def preferred_width(self, max_available_width):
                # Rich wraps to the allocated width; measuring the entire text
                # here is both unnecessary and expensive for long transcripts.
                return max_available_width

            def preferred_height(self, width, max_available_height, wrap_lines, get_line_prefix):
                return len(pane.lines(width))

            def create_content(self, width, height):
                lines = pane.lines(width)
                if pane._anchor is not None:
                    # Line offsets depend on the wrap width, which only the
                    # render knows, so a requested anchor is applied here.
                    # Clamped: an empty last renderable starts past the final
                    # line, since trailing newlines are stripped, and the
                    # window reads the cursor row as a real line.
                    offset = pane.line_offset(pane._anchor, width)
                    pane.window.vertical_scroll = min(offset, max(0, len(lines) - 1))
                    pane._anchor = None
                return UIContent(
                    get_line=lines.__getitem__,
                    line_count=len(lines),
                    show_cursor=False,
                    cursor_position=Point(0, pane.window.vertical_scroll),
                )

        # The window scrolls to keep the reported cursor visible on every render,
        # so a fixed row 0 would snap keyboard scrolling straight back to the top.
        self.control = Control()
        self.window = Window(
            self.control, wrap_lines=False, right_margins=[ScrollbarMargin(display_arrows=True)]
        )

    def follow(self, renderables: list) -> None:
        """Replace streaming content: stay on the tail if the reader was there, else hold."""
        info = self.window.render_info
        offset = self.window.vertical_scroll
        tailing = info is not None and offset >= max(0, info.content_height - info.window_height)
        self.set(renderables)
        if info is not None:
            rows = len(self.lines(info.window_width))
            self.window.vertical_scroll = max(0, rows - info.window_height) if tailing else offset

    def set(self, renderables: list, *, anchor: int | None = None) -> None:
        """Replace the content, scrolled to the top or to ``renderables[anchor]``."""
        self.renderables = renderables
        self._version += 1
        self.window.vertical_scroll = 0
        self.scroll_to(anchor)

    def scroll_to(self, index: int | None) -> None:
        """Put the start of ``renderables[index]`` on the pane's top row at the next render."""
        self._anchor = index

    def line_offset(self, index: int, width: int) -> int:
        """First rendered line of ``renderables[index]`` at ``width``."""
        self.fragments(width)
        offsets = self._cache[3]
        return offsets[min(max(index, 0), len(offsets) - 1)] if offsets else 0

    def fragments(self, width: int) -> list:
        key = (self._version, width)
        if self._cache is None or self._cache[:2] != key:
            console = Console(
                file=StringIO(),
                force_terminal=True,
                color_system=self.color_system,
                width=max(1, width),
                theme=self.theme,
                highlight=False,
            )
            offsets, lines, position = [], 0, 0
            for renderable in self.renderables:
                offsets.append(lines)
                console.print(renderable)
                # Read only the new output; re-reading the whole buffer per
                # renderable would be quadratic on a long transcript.
                console.file.seek(position)
                lines += console.file.read().count("\n")
                position = console.file.tell()
            rendered = OSC.sub("", console.file.getvalue().rstrip("\n"))
            self._cache = (*key, to_formatted_text(ANSI(rendered)), offsets)
        return self._cache[2]

    def lines(self, width: int) -> list:
        key = (self._version, width)
        if self._line_cache is None or self._line_cache[:2] != key:
            self._line_cache = (*key, list(split_lines(self.fragments(width))))
        return self._line_cache[2]

    def text(self, width: int = 80) -> str:
        """Unstyled rendering without Rich's line padding, for tests and logs."""
        lines = fragment_list_to_text(self.fragments(width)).splitlines()
        return "\n".join(line.rstrip() for line in lines)

    def __pt_container__(self):
        return self.window

    def bind_scrolling(self, keys, *, paging: Filter | None = None) -> None:
        """Arrow and page keys scroll the pane while it has focus.

        ``paging`` also gives it PageUp/PageDown from elsewhere, e.g. from a
        docked editor, so the reader can scroll an answer while replying to it.
        """
        focused = has_focus(self.window)
        pages = focused | paging if paging is not None else focused

        def scroll(direction: int, page: bool = False, half: bool = False):
            def handler(event):
                info = self.window.render_info
                if info is None:
                    return
                rows = (
                    max(1, info.window_height // 2)
                    if half
                    else (max(1, info.window_height - 1) if page else 1)
                )
                bottom = max(0, info.content_height - info.window_height)
                self.window.vertical_scroll = max(
                    0, min(bottom, self.window.vertical_scroll + direction * rows)
                )

            return handler

        keys.add("up", filter=focused)(scroll(-1))
        keys.add("down", filter=focused)(scroll(1))
        keys.add("pageup", filter=pages)(scroll(-1, page=True))
        keys.add("pagedown", filter=pages)(scroll(1, page=True))
        keys.add("c-u", filter=focused)(scroll(-1, half=True))
        keys.add("c-d", filter=focused)(scroll(1, half=True))


def list_pane_height(rows: int = LIST_ROWS_MAX) -> Dimension:
    """Height for a framed selector stacked above a scrolling detail pane.

    ``Dimension`` defaults ``preferred`` to ``min``, and ``HSplit`` gives every
    child its preferred height before letting any of them grow beyond it. With
    no explicit preferred, a long diff or tool payload squeezes the selector
    down to its minimum and the list effectively disappears, so ask for the
    rows up front.
    """
    visible = max(LIST_ROWS_MIN, min(LIST_ROWS_MAX, rows))
    return Dimension(
        min=1 + _BORDERS,
        preferred=visible + _BORDERS,
        max=LIST_ROWS_MAX + _BORDERS,
    )


class PopupInput:
    """A message editor docked in a popup: type, Enter sends, Esc goes back.

    The popup decides what sending means. ``submit`` receives the draft and
    either accepts it, which clears the draft, or raises ``ValueError`` to
    refuse, which keeps the draft and shows why in the editor's title.

    The editor's own keys are scoped to it, so its Enter and Esc win over the
    popup's while it has focus. The popup's one-letter shortcuts are not
    scoped, and a letter binding outranks typing: gate them on ``browsing``,
    or pressing ``c`` in the editor copies instead of typing a ``c``.

    Focus it with ``open``. A popup should not open with focus here: one that
    appears on its own could swallow keystrokes meant for the main prompt, and
    Enter would then send them.
    """

    def __init__(
        self,
        submit: Callable[[str], None],
        *,
        home,
        title: AnyFormattedText = "Message",
        placeholder: str = "",
    ) -> None:
        # Ctrl+J and Shift+Enter arrive as terminal-specific sequences; the
        # main prompt registers them too, but a popup can open without it.
        configure_newline_keys()
        self.submit = submit
        # Where Esc returns focus: the popup's list, usually.
        self.home = home
        self.title = title
        self.notice = ""
        self.area = TextArea(
            multiline=True,
            wrap_lines=True,
            focus_on_click=True,
            prompt=_INPUT_PROMPT,
            height=self.rows,
            input_processors=[
                ConditionalProcessor(
                    AfterInput(placeholder, style="class:placeholder"),
                    Condition(lambda: not self.area.text),
                )
            ],
        )
        self.area.buffer.on_text_changed += lambda _: self._clear_notice()
        self.editing = has_focus(self.area)
        self.browsing = ~self.editing
        keys = KeyBindings()

        @keys.add("enter")
        def send(event):
            self.send()

        @keys.add("c-j")
        def newline(event):
            self.area.buffer.newline(copy_margin=False)

        @keys.add("escape", eager=True)
        def leave(event):
            # The draft stays: Esc steps out to browse, it does not discard.
            event.app.layout.focus(self.home)

        frame = Frame(self.area, title=self._title)
        self.container = HSplit([frame], key_bindings=keys)

    def __pt_container__(self):
        return self.container

    @property
    def text(self) -> str:
        return self.area.text

    def open(self, app) -> None:
        app.layout.focus(self.area)

    def send(self) -> bool:
        """Hand the draft to ``submit``; whether it was accepted."""
        text = self.area.text.strip()
        if not text:
            return False
        try:
            self.submit(text)
        except ValueError as error:
            self.notice = str(error)
            return False
        self.area.buffer.reset()
        self.notice = ""
        return True

    def rows(self) -> Dimension:
        """Exactly as tall as the draft, up to ``INPUT_ROWS_MAX``, then it scrolls.

        An open-ended height would take a share of the popup's spare rows and
        sit mostly empty under the answer it is replying to.
        """
        info = self.area.window.render_info
        width = max(1, (info.window_width if info else 80) - get_cwidth(_INPUT_PROMPT))
        rows = sum(max(1, -(-get_cwidth(line) // width)) for line in self.area.text.split("\n"))
        return Dimension.exact(min(INPUT_ROWS_MAX, rows))

    def _title(self):
        title = fragment_list_to_text(to_formatted_text(self.title))
        return f"{title} · {self.notice}" if self.notice else title

    def _clear_notice(self) -> None:
        self.notice = ""
