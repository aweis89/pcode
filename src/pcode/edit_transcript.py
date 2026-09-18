"""Literal, width-aware edit blocks, independent of assistant Markdown."""

from dataclasses import dataclass
from functools import cache

from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.lexers import Lexer
from pygments.token import Generic, Token
from rich.console import Console, ConsoleOptions, RenderResult
from rich.rule import Rule
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text

from pcode.edits import edit_text
from pcode.runtime import EditCompleted
from pcode.tool_display import command_text


@dataclass(frozen=True)
class EditTranscript:
    change: EditCompleted
    code_theme: str = "monokai"
    max_rows: int = 60

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        change = self.change
        yield Rule(style="pcode.muted")
        yield Text(
            f"✓ {change.operation.capitalize()} {edit_text(change.path)}"
            f" · +{change.added} −{change.removed}",
            style="pcode.accent",
        )
        if change.patch:
            patch = Syntax(
                edit_text(change.patch),
                "diff",
                theme=self.code_theme,
                word_wrap=True,
                background_color="default",
            )
            rows = console.render_lines(patch, options, pad=False)
            for row in rows[: self.max_rows]:
                yield from row
                yield Segment.line()
            if len(rows) > self.max_rows or change.truncated:
                yield Text("… additional diff rows omitted", style="pcode.muted")
        if change.omitted:
            yield Text(f"Diff unavailable: {edit_text(change.omitted)}", style="pcode.muted")
        yield Rule(style="pcode.muted")


@cache
def _token_style(code_theme: str, token) -> str:
    """Render one syntax token as a prompt_toolkit style string."""
    style = Syntax.get_theme(code_theme).get_style_for_token(token)
    # Convert only a library-generated marker, never file content. This keeps
    # ANSI palette colors native while matching Rich's RGB syntax colors too.
    marker = Style(color=style.color, bold=style.bold).render("x", color_system="truecolor")
    return to_formatted_text(ANSI(marker))[0][0]


@cache
def _preview_style(code_theme: str, added: bool) -> str:
    token = Generic.Inserted if added else Generic.Deleted
    return "class:bottom-toolbar.text " + _token_style(code_theme, token)


def diff_token(line: str):
    """Classify a logical diff line the way Pygments' diff lexer does."""
    if line.startswith(("+", "> ")):
        return Generic.Inserted
    if line.startswith(("-", "< ")):
        return Generic.Deleted
    if line.startswith("@"):
        return Generic.Subheading
    if line.startswith(("diff", "index", "Index:", "=")):
        return Generic.Heading
    if line.startswith("!"):
        return Generic.Strong
    return Token.Text


class DiffLexer(Lexer):
    """Color diffs in prompt_toolkit with the same theme scrollback diffs use."""

    def __init__(self, code_theme: str = "monokai") -> None:
        self.code_theme = code_theme

    def lex_document(self, document):
        def line(number: int):
            text = document.lines[number]
            return [(_token_style(self.code_theme, diff_token(text)), text)]

        return line


def edit_preview_rows(text: str, width: int, code_theme: str) -> list[tuple[str, str]]:
    """Color logical +/- lines before wrapping so continuation rows keep their color."""
    width = max(1, width)
    console = Console(width=width)
    rows = []
    for line in command_text(text).split("\n"):
        style = (
            _preview_style(code_theme, line.startswith("+"))
            if line.startswith(("+", "-"))
            else "class:bottom-toolbar.text"
        )
        rows.extend(
            (style, row.plain)
            for row in Text(line).wrap(console, width, overflow="fold", no_wrap=False)
        )
    return rows
