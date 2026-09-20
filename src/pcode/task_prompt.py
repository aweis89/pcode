"""Literal, quote-styled submitted prompts."""

from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.text import Text


@dataclass(frozen=True)
class TaskPrompt:
    text: str

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        # Popups render prompts on a console that may carry no pcode theme.
        style = console.get_style("pcode.accent", default="none")
        # Leave room for a quote rail on every physical line, including wraps.
        # On one-column terminals prioritize the text rather than the decoration.
        prefix = "▌ " if options.max_width > 2 else ""
        lines = console.render_lines(
            Text(self.text, style=style),
            options.update(width=max(1, options.max_width - len(prefix))),
            pad=False,
        )
        for line in lines:
            yield Segment(prefix, style)
            yield from line
            yield Segment.line()
