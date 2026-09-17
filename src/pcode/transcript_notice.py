"""Lightweight, literal diagnostics for permanent terminal scrollback."""

from dataclasses import dataclass
from typing import Literal

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.text import Text


@dataclass(frozen=True)
class TranscriptNotice:
    text: str
    kind: Literal["error", "warning", "cancelled"]
    title: str

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        symbol = "✗" if self.kind == "error" else "!"
        style = "pcode.error" if self.kind == "error" else "pcode.warning"
        message = Text(f"{symbol} {self.title}", style=style)
        yield message
        if self.text:
            indent = "  " if options.max_width > 2 else ""
            for line in console.render_lines(
                Text(self.text),
                options.update(width=max(1, options.max_width - len(indent))),
                pad=False,
            ):
                yield Segment(indent)
                yield from line
                yield Segment.line()
