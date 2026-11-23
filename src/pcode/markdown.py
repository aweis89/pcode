"""Rich Markdown that also renders local ``file://`` links."""

import re

from markdown_it import MarkdownIt
from markdown_it.common.normalize_url import validateLink as _default_validate_link
from rich.console import Console, ConsoleOptions, JustifyMethod, RenderResult
from rich.markdown import CodeBlock, HorizontalRule
from rich.markdown import Markdown as RichMarkdown
from rich.rule import Rule
from rich.style import Style
from rich.text import Text

from pcode.block import RULE
from pcode.row_marks import row_mark
from pcode.smooth_scroll import MARK_BLOCK

_FILE_URL = re.compile(r"^file://", re.IGNORECASE)


def _validate_link(url: str) -> bool:
    if _FILE_URL.match(url.strip()):
        return True
    return _default_validate_link(url)


_PARSER = MarkdownIt().enable("strikethrough").enable("table")
_PARSER.validateLink = _validate_link  # type: ignore[method-assign]


class _BlockCodeBlock(CodeBlock):
    """A code block whose first row is a reader-mode block mark (`pcode.row_marks`)."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield from row_mark(MARK_BLOCK)
        yield from super().__rich_console__(console, options)


class _SolidHorizontalRule(HorizontalRule):
    """A ``---`` rule drawn with pcode's box-drawing `RULE`, not Rich's ASCII ``-``.

    An ASCII hyphen sits centred in its cell, so a row of them reads as dashed;
    box-drawing characters fill the cell and join into the same solid line
    pcode's own block separators use.
    """

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield Rule(style=console.get_style("markdown.hr", default="none"), characters=RULE)
        yield Text()


class Markdown(RichMarkdown):
    """Rich Markdown that accepts ``file://`` URLs alongside ``http(s)://``."""

    elements = {
        **RichMarkdown.elements,
        "fence": _BlockCodeBlock,
        "code_block": _BlockCodeBlock,
        "hr": _SolidHorizontalRule,
    }

    def __init__(
        self,
        markup: str,
        code_theme: str = "monokai",
        justify: JustifyMethod | None = None,
        style: str | Style = "none",
        hyperlinks: bool = True,
        inline_code_lexer: str | None = None,
        inline_code_theme: str | None = None,
    ) -> None:
        self.markup = markup
        self.parsed = _PARSER.parse(markup)
        self.code_theme = code_theme
        self.justify: JustifyMethod | None = justify
        self.style = style
        self.hyperlinks = hyperlinks
        self.inline_code_lexer = inline_code_lexer
        self.inline_code_theme = inline_code_theme or code_theme
