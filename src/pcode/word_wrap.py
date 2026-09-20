"""Word-aware soft wrapping for the editor buffer.

prompt_toolkit wraps a buffer line strictly by column: the character that no
longer fits moves to the next row, so a word straddles the wrap point. There is
no word-wrap option on ``Window``/``BufferControl`` -- the break is decided
character by character deep inside ``Window._copy_body``.

Rather than reimplement that rendering, this pads the *display* text: when a
word would straddle the wrap point, enough spaces are inserted before it to
fill the current row, so prompt_toolkit's own column wrapping lands on the word
boundary. The buffer text is untouched; the cursor mappings below keep editing
positions correct, the same technique ``TabsProcessor`` uses to expand tabs.
"""

from __future__ import annotations

from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.utils import get_cwidth

__all__ = ["WordWrapProcessor", "wrap_padding"]


def wrap_padding(text: str, width: int) -> dict[int, int]:
    """Map index -> number of spaces to insert before ``text[index]``.

    ``width`` is the number of columns available for the text itself (the line
    prefix is already subtracted). Words wider than a full row are left alone:
    they have to be split somewhere, and padding them would only waste a row.
    """
    if width < 2 or not text:
        return {}

    padding: dict[int, int] = {}
    column = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        starts_word = not char.isspace() and (index == 0 or text[index - 1].isspace())
        # A word starting at column 0, or one whose row is already full, wraps
        # correctly on its own.
        if starts_word and 0 < column < width:
            end = index
            while end < length and not text[end].isspace():
                end += 1
            word = get_cwidth(text[index:end])
            if column + word > width >= word:
                padding[index] = width - column
                column = 0
        char_width = get_cwidth(char)
        if column + char_width > width:
            column = 0
        column += char_width
        index += 1
    return padding


class WordWrapProcessor(Processor):
    """Pad display lines so soft wraps fall between words.

    :param prefix_width: Columns taken by the window's line prefix (the prompt
        and its continuation), which ``TransformationInput.width`` includes.
    """

    def __init__(self, prefix_width: int = 0) -> None:
        self.prefix_width = prefix_width

    def apply_transformation(self, ti: TransformationInput) -> Transformation:
        fragments = explode_text_fragments(ti.fragments)
        padding = wrap_padding(
            "".join(text for _, text, *_ in fragments), ti.width - self.prefix_width
        )
        if not padding:
            return Transformation(ti.fragments)

        result = []
        positions = {}
        position = 0
        for index, fragment in enumerate(fragments):
            pad = padding.get(index)
            if pad:
                result.append(("", " " * pad))
                position += pad
            positions[index] = position
            result.append(fragment)
            position += 1
        # The cursor can also sit right after the line, and one past that.
        positions[len(fragments)] = position
        positions[len(fragments) + 1] = position + 1

        def source_to_display(from_position: int) -> int:
            return positions.get(from_position, from_position)

        def display_to_source(display_position: int) -> int:
            reversed_positions = {v: k for k, v in positions.items()}
            while display_position >= 0:
                if display_position in reversed_positions:
                    return reversed_positions[display_position]
                display_position -= 1
            return 0

        return Transformation(
            result,
            source_to_display=source_to_display,
            display_to_source=display_to_source,
        )
