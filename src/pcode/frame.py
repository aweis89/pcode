"""pcode's framed boxes: a heading on the top rule and an optional footer below.

prompt_toolkit's own ``Frame`` centers its title between bars
(``┌──| Title |──┐``) and has no footer. These read ``┌─ Title ────┐``, the
way the task panel above the prompt draws its heading, and can carry a short
footer such as the keys that close an overlay: ``└──── Esc cancel ─┘``.
"""

from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.formatted_text import AnyFormattedText, to_formatted_text
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
from prompt_toolkit.layout.dimension import AnyDimension
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Box, Label, Shadow
from prompt_toolkit.widgets.base import Border

# Columns a heading or footer takes beyond its text: the corners, the rule
# stub beside the text, and a space either side. See ``Frame``.
TITLE_CHROME = 5


def text_width(text: AnyFormattedText) -> int:
    return get_cwidth(fragment_list_to_text(to_formatted_text(text)))


class Frame:
    """A box around ``body``, drop-in for prompt_toolkit's ``Frame``.

    ``title`` and ``footer`` may be callables, read each render; an empty one
    leaves a plain rule.
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
    ) -> None:
        self.title = title
        self.footer = footer
        self.body = body

        def border(char: str = Border.HORIZONTAL, width: int | None = None) -> Window:
            return Window(char=char, width=width, height=1, style="class:frame.border")

        def text(get, style: str) -> Label:
            # Reads the attribute each render, so a caller may reassign it.
            return Label(
                lambda: [("", " "), *to_formatted_text(get()), ("", " ")],
                style=style,
                dont_extend_width=True,
            )

        def shown(get) -> Condition:
            return Condition(lambda: bool(fragment_list_to_text(to_formatted_text(get()))))

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
            filter=shown(lambda: self.title),
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
            filter=shown(lambda: self.footer),
            alternative_content=VSplit(
                [border(Border.BOTTOM_LEFT, 1), border(), border(Border.BOTTOM_RIGHT, 1)],
                height=1,
            ),
        )
        middle = VSplit(
            [
                Window(width=1, char=Border.VERTICAL, style="class:frame.border"),
                DynamicContainer(lambda: self.body),
                Window(width=1, char=Border.VERTICAL, style="class:frame.border"),
            ],
            padding=0,
        )
        self.container = HSplit(
            [top, middle, bottom],
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
    a background), drawn with this module's ``Frame``.
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
            key_bindings=keys,
            modal=True,
        )
        self.container = Box(body=Shadow(body=frame), style="class:dialog")

    def __pt_container__(self) -> AnyContainer:
        return self.container
