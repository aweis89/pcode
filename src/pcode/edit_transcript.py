"""Literal, width-aware edit blocks, independent of assistant Markdown."""

from dataclasses import dataclass
from functools import cache

from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.lexers import Lexer
from pygments.token import Generic, Token
from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text

from pcode.block import DONE, block_heading, block_rule
from pcode.delta import Delta
from pcode.edits import edit_text
from pcode.runtime import EditCompleted
from pcode.syntax import transparent_theme
from pcode.tool_display import command_text


@dataclass(frozen=True)
class EditTranscript:
    change: EditCompleted
    code_theme: str = "monokai"
    max_rows: int = 60
    delta: Delta | None = None

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        change = self.change
        yield block_rule(
            block_heading(
                DONE,
                f"{change.operation.capitalize()} {edit_text(change.path)}"
                f" · +{change.added} −{change.removed}",
            )
        )
        if change.patch:
            text = edit_text(change.patch)
            lines = self.delta.render(text, options.max_width) if self.delta else None
            patch = (
                Text("\n").join(lines)
                if lines is not None
                else Syntax(text, "diff", theme=transparent_theme(self.code_theme), word_wrap=True)
            )
            rows = console.render_lines(patch, options, pad=False)
            for row in rows[: self.max_rows]:
                yield from row
                yield Segment.line()
            if len(rows) > self.max_rows or change.truncated:
                yield Text("… additional diff rows omitted", style="pcode.muted")
        if change.omitted:
            yield Text(f"Diff unavailable: {edit_text(change.omitted)}", style="pcode.muted")
        yield block_rule()


def prefetch_edits(objects, width: int) -> None:
    """Render every delta edit block in `objects` through one delta process each."""
    patches: dict[Delta, list[str]] = {}
    for obj in objects:
        if isinstance(obj, EditTranscript) and obj.delta is not None and obj.change.patch:
            patches.setdefault(obj.delta, []).append(edit_text(obj.change.patch))
    for delta, texts in patches.items():
        delta.prefetch(texts, width)


@cache
def _token_style(code_theme: str, token) -> str:
    """Render one syntax token as a prompt_toolkit style string."""
    style = transparent_theme(code_theme).get_style_for_token(token)
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


@cache
def _rich_style(style: Style) -> str:
    """A Rich style as a prompt_toolkit style string, by way of its ANSI codes."""
    if not style:
        return ""
    # A link would open with an OSC 8 sequence ahead of the marker's colors.
    marker = style.clear_meta_and_links().render("x")
    return to_formatted_text(ANSI(marker))[0][0]


_FRAGMENT_CONSOLE = Console(width=10_000, color_system="truecolor", force_terminal=True)


def text_fragments(text: Text) -> list[tuple[str, str]]:
    """One unwrapped Rich line as prompt_toolkit fragments."""
    console = _FRAGMENT_CONSOLE
    # Console.render, unlike Text.render, applies the text's own base style.
    options = console.options.update(no_wrap=True, overflow="ignore")
    return [
        (_rich_style(segment.style), segment.text)
        for segment in console.render(text, options)
        if segment.text and segment.text != "\n"
    ]


class DiffLexer(Lexer):
    """Color diffs in prompt_toolkit with the same theme scrollback diffs use.

    `rows` holds fragments already styled elsewhere (delta's output), by row;
    every other row is classified as a plain diff line.
    """

    def __init__(self, code_theme: str = "monokai") -> None:
        self.code_theme = code_theme
        self._rows: dict[int, list[tuple[str, str]]] = {}
        self._version = 0

    @property
    def rows(self) -> dict[int, list[tuple[str, str]]]:
        return self._rows

    @rows.setter
    def rows(self, rows: dict[int, list[tuple[str, str]]]) -> None:
        self._rows = rows
        self._version += 1

    def lex_document(self, document):
        rows = self.rows

        def line(number: int):
            if number in rows:
                return rows[number]
            text = document.lines[number]
            return [(_token_style(self.code_theme, diff_token(text)), text)]

        return line

    def invalidation_hash(self):
        return self._version


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
