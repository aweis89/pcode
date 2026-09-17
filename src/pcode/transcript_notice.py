"""Lightweight, literal diagnostics for permanent terminal scrollback."""

from dataclasses import dataclass
from typing import Literal

from rich.console import Console, ConsoleOptions, RenderResult
from rich.highlighter import ReprHighlighter
from rich.segment import Segment
from rich.text import Text


@dataclass(frozen=True)
class TranscriptNotice:
    text: str
    kind: Literal["error", "warning", "cancelled"]
    title: str
    max_lines: int | None = None

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        symbol = "✗" if self.kind == "error" else "!"
        style = "pcode.error" if self.kind == "error" else "pcode.warning"
        message = Text(f"{symbol} {self.title}", style=style)
        yield message
        if self.text:
            indent = "  " if options.max_width > 2 else ""
            # Highlight literal log text; never interpret diagnostics as markup.
            body = ReprHighlighter()(self.text) if self.kind == "error" else Text(self.text)
            lines = console.render_lines(
                body,
                options.update(width=max(1, options.max_width - len(indent))),
                pad=False,
            )
            if self.max_lines is not None and len(lines) > self.max_lines:
                # Count wrapped terminal rows, including the omission marker. Keep
                # the tail, where exceptions and command failures usually explain why.
                marker = Text("… earlier error output truncated", style="pcode.muted")
                marker.truncate(max(1, options.max_width - len(indent)), overflow="ellipsis")
                yield Segment(indent)
                yield from console.render(marker, options)
                lines = lines[-(self.max_lines - 1) :] if self.max_lines > 1 else []
            for line in lines:
                yield Segment(indent)
                yield from line
                yield Segment.line()
