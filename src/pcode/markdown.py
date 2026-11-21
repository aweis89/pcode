"""Rich Markdown that also renders local ``file://`` links."""

import re

from markdown_it import MarkdownIt
from markdown_it.common.normalize_url import validateLink as _default_validate_link
from rich.console import JustifyMethod
from rich.markdown import Markdown as RichMarkdown
from rich.style import Style

_FILE_URL = re.compile(r"^file://", re.IGNORECASE)


def _validate_link(url: str) -> bool:
    if _FILE_URL.match(url.strip()):
        return True
    return _default_validate_link(url)


_PARSER = MarkdownIt().enable("strikethrough").enable("table")
_PARSER.validateLink = _validate_link  # type: ignore[method-assign]


class Markdown(RichMarkdown):
    """Rich Markdown that accepts ``file://`` URLs alongside ``http(s)://``."""

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
