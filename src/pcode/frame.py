"""pcode's framed boxes: a heading on the top rule and an optional footer below.

prompt_toolkit's own ``Frame`` centers its title between bars
(``┌──| Title |──┐``) and has no footer. These read ``┌─ Title ────┐``, the
way the task panel above the prompt draws its heading, and can carry a short
footer such as the keys that close an overlay: ``└──── Esc cancel ─┘``.

Drawn in a native overlay (``popup_view overlay``), whose panel is already a
rounded box with a header, a second box inside it would only double up. There
a ``Frame`` drops its corners and sides, keeping a heading rule
(`` Title ──────``) and its footer text, a ``Dialog`` its box altogether, and a
scroll bar becomes a thin thumb (``SlimScrollbarMargin``). Which is drawn is
decided at each render, so one widget serves both.
"""

from prompt_toolkit.filters import Condition, Never, has_completions
from prompt_toolkit.formatted_text import (
    AnyFormattedText,
    StyleAndTextTuples,
    to_formatted_text,
)
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import (
    AnyContainer,
    ConditionalContainer,
    DynamicContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.containers import WindowRenderInfo
from prompt_toolkit.layout.dimension import AnyDimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Box, Label, Shadow, TextArea
from prompt_toolkit.widgets.base import Border

from pcode.popup_surface import drawn_in_overlay, set_title

# Columns a heading or footer takes beyond its text: the corners, the rule
# stub beside the text, and a space either side. See ``Frame``.
TITLE_CHROME = 5
# Columns a frame takes beside its body: two borders, or in an overlay the
# one-column pad either side that keeps side-by-side panes apart.
FRAME_COLUMNS = 2

in_overlay = Condition(drawn_in_overlay)


def text_width(text: AnyFormattedText) -> int:
    return get_cwidth(fragment_list_to_text(to_formatted_text(text)))


def frame_rows(*, footer: bool = False) -> int:
    """Rows a titled ``Frame`` takes beyond its body, as drawn now.

    Boxed, its top and bottom rules; in an overlay, its heading rule and only
    a footer's row if it has one (untitled, it has no heading row
    there either). Read at render, like the frame itself.
    """
    if drawn_in_overlay():
        return 1 + footer
    return 2


class Frame:
    """A box around ``body``, drop-in for prompt_toolkit's ``Frame``.

    ``title`` and ``footer`` may be callables, read each render; an empty one
    leaves a plain rule. ``boxed`` keeps the box even in an overlay, for a
    frame floating over other content.
    """

    def __init__(
        self,
        body: AnyContainer,
        title: AnyFormattedText = "",
        style: str = "",
        width: AnyDimension = None,
        height: AnyDimension = None,
        key_bindings: KeyBindings | None = None,
        modal: bool = False,
        footer: AnyFormattedText = "",
        boxed: bool = False,
    ) -> None:
        self.title = title
        self.footer = footer
        self.body = body

        def border(char: str = Border.HORIZONTAL, width: int | None = None) -> Window:
            return Window(char=char, width=width, height=1, style="class:frame.border")

        def pad() -> Window:
            return Window(width=1)

        def text(get, style: str, lead: str = " ", trail: str = " ") -> Label:
            # Reads the attribute each render, so a caller may reassign it.
            return Label(
                lambda: [("", lead), *to_formatted_text(get()), ("", trail)],
                style=style,
                dont_extend_width=True,
            )

        def shown(get) -> Condition:
            return Condition(lambda: bool(fragment_list_to_text(to_formatted_text(get()))))

        titled, footed = shown(lambda: self.title), shown(lambda: self.footer)
        flat = in_overlay if not boxed else Never()

        top = ConditionalContainer(
            VSplit(
                [
                    border(Border.TOP_LEFT, 1),
                    border(width=1),
                    text(lambda: self.title, "class:frame.label"),
                    border(),
                    border(Border.TOP_RIGHT, 1),
                ],
                height=1,
            ),
            filter=titled,
            alternative_content=VSplit(
                [border(Border.TOP_LEFT, 1), border(), border(Border.TOP_RIGHT, 1)], height=1
            ),
        )
        bottom = ConditionalContainer(
            VSplit(
                [
                    border(Border.BOTTOM_LEFT, 1),
                    border(),
                    text(lambda: self.footer, "class:frame.footer"),
                    border(width=1),
                    border(Border.BOTTOM_RIGHT, 1),
                ],
                height=1,
            ),
            filter=footed,
            alternative_content=VSplit(
                [border(Border.BOTTOM_LEFT, 1), border(), border(Border.BOTTOM_RIGHT, 1)],
                height=1,
            ),
        )

        def side() -> ConditionalContainer:
            return ConditionalContainer(
                Window(width=1, char=Border.VERTICAL, style="class:frame.border"),
                filter=~flat,
                alternative_content=pad(),
            )

        # The body appears once in the tree, whichever way it is drawn: a
        # container under two parents confuses focus and key binding lookup.
        middle = VSplit([side(), DynamicContainer(lambda: self.body), side()], padding=0)
        # In an overlay: ` Title ──────` over the body, the body between
        # one-column pads, and the footer's text alone, right-aligned.
        heading = ConditionalContainer(
            VSplit(
                [pad(), text(lambda: self.title, "class:frame.label", lead=""), border(), pad()],
                height=1,
            ),
            filter=titled,
        )
        ending = ConditionalContainer(
            VSplit(
                [
                    Window(height=1),
                    text(lambda: self.footer, "class:frame.footer dim", trail=""),
                    pad(),
                ],
                height=1,
            ),
            filter=footed,
        )
        self.container = HSplit(
            [
                ConditionalContainer(top, filter=~flat, alternative_content=heading),
                middle,
                ConditionalContainer(bottom, filter=~flat, alternative_content=ending),
            ],
            width=width,
            height=height,
            style="class:frame " + style,
            key_bindings=key_bindings,
            modal=modal,
        )

    def __pt_container__(self) -> AnyContainer:
        return self.container


class Dialog:
    """A modal framed box over a background, as pcode's popups use it.

    The subset of prompt_toolkit's ``Dialog`` used here (no buttons, always on
    a background), drawn with this module's ``Frame``. In an overlay its body
    fills the panel unboxed, and its title heads the panel instead.
    """

    def __init__(self, body: AnyContainer, title: AnyFormattedText = "") -> None:
        self.body = body
        self.title = title
        keys = KeyBindings()
        keys.add("tab", filter=~has_completions)(focus_next)
        keys.add("s-tab", filter=~has_completions)(focus_previous)
        frame = Frame(
            DynamicContainer(lambda: self.body),
            title=lambda: self.title,
            style="class:dialog.body",
        )

        def bare() -> AnyContainer:
            # Asked at each render in an overlay: the header follows the title.
            set_title(fragment_list_to_text(to_formatted_text(self.title)))
            return self.body

        framed = Box(body=Shadow(body=frame), style="class:dialog")
        unframed = HSplit([DynamicContainer(bare)], style="class:dialog class:dialog.body")
        # Only the one drawn is in the tree (see `Frame`'s body).
        self.container = HSplit(
            [DynamicContainer(lambda: unframed if drawn_in_overlay() else framed)],
            key_bindings=keys,
            modal=True,
        )

    def __pt_container__(self) -> AnyContainer:
        return self.container


class SlimScrollbarMargin(ScrollbarMargin):
    """prompt_toolkit's scroll bar with arrows, or in an overlay a thin thumb.

    The thumb (``▕``) shows only while the content overflows, with no track
    or arrows. Either way it is one column wide; the window scrolls as before.
    """

    def __init__(self) -> None:
        super().__init__(display_arrows=True)

    def create_margin(
        self, window_render_info: WindowRenderInfo, width: int, height: int
    ) -> StyleAndTextTuples:
        if not drawn_in_overlay():
            return super().create_margin(window_render_info, width, height)
        content = window_render_info.content_height
        lines = window_render_info.displayed_lines
        if not lines or (window_render_info.vertical_scroll == 0 and lines[-1] >= content - 1):
            return []
        # A wrapped line fills several rows: count lines, as `content` does.
        shown = len(set(lines))
        rows = window_render_info.window_height
        thumb = min(rows, max(1, round(rows * shown / content)))
        # The thumb reaches the bottom exactly when the last line is shown.
        scrollable = max(1, content - shown)
        top = round((rows - thumb) * min(1.0, window_render_info.vertical_scroll / scrollable))
        result: StyleAndTextTuples = []
        for row in range(rows):
            if row:
                result.append(("", "\n"))
            if top <= row < top + thumb:
                result.append(("class:scrollbar.slim", "\u2595"))
            else:
                result.append(("", " "))
        return result


class ScrollingTextArea(TextArea):
    """A ``TextArea`` whose ``scrollbar=True`` draws a ``SlimScrollbarMargin``."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.window.right_margins = [
            SlimScrollbarMargin() if type(margin) is ScrollbarMargin else margin
            for margin in self.window.right_margins
        ]
