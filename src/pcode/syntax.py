"""Syntax highlighting rendered on the terminal's own background."""

from functools import cache

from pygments.token import _TokenType
from rich.style import Style
from rich.syntax import Syntax, SyntaxTheme


class TransparentSyntaxTheme(SyntaxTheme):
    """A Pygments style with its backgrounds dropped rather than painted over.

    Passing ``background_color="default"`` to :class:`~rich.syntax.Syntax`
    paints every token background, not just the block's, and some styles carry
    meaning there: gruvbox, the default for both palettes, colours diff +/−
    lines by background and sets their foreground to the style's own background
    colour. Flattened that way a diff loses every marker colour and reads as
    unhighlighted text. Fold a meaningful background into the foreground
    instead, and drop the backgrounds that only repeat the block's.
    """

    def __init__(self, code_theme: str) -> None:
        self.theme = Syntax.get_theme(code_theme)
        self.background = self.theme.get_background_style().bgcolor

    def get_background_style(self) -> Style:
        return Style()

    def get_style_for_token(self, token_type: _TokenType) -> Style:
        style = self.theme.get_style_for_token(token_type)
        color = style.color if style.bgcolor in (None, self.background) else style.bgcolor
        return Style(
            color=color,
            bold=style.bold,
            italic=style.italic,
            underline=style.underline,
        )


@cache
def transparent_theme(code_theme: str) -> TransparentSyntaxTheme:
    """The named style, resolved once, with its backgrounds left transparent."""
    return TransparentSyntaxTheme(code_theme)
