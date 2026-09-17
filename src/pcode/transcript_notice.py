"""Literal diagnostics with fenced Markdown error logs for terminal scrollback."""

import re
from dataclasses import dataclass
from typing import Literal

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Markdown
from rich.segment import Segment
from rich.text import Text


@dataclass(frozen=True)
class TranscriptNotice:
    text: str
    kind: Literal["error", "warning", "cancelled"]
    title: str
    max_lines: int | None = None
    code_theme: str = "monokai"

    def _code_block(self, text: str) -> Markdown:
        # Logs may themselves contain fences. A longer fence keeps everything
        # literal, including Markdown headings, links, and embedded backticks.
        longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
        fence = "`" * max(3, longest + 1)
        return Markdown(f"{fence}text\n{text}\n{fence}", code_theme=self.code_theme)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        symbol = "✗" if self.kind == "error" else "!"
        style = "pcode.error" if self.kind == "error" else "pcode.warning"
        yield Text(f"{symbol} {self.title}", style=style)
        if not self.text:
            return
        # Error code-block backgrounds start at the terminal edge; keep Rich
        # padding and the log's own indentation inside the block unchanged.
        indent = "  " if self.kind != "error" and options.max_width > 2 else ""
        body_options = options.update(width=max(1, options.max_width - len(indent)))
        # Rich's Markdown code blocks have one padding row/column on each side.
        # Fall back to literal text only when the pane cannot fit that padding.
        fenced = self.kind == "error" and body_options.max_width > 2
        body = self._code_block(self.text) if fenced else Text(self.text)
        lines = console.render_lines(body, body_options, pad=False)
        top, bottom = (lines[:1], lines[-1:]) if fenced else ([], [])
        if fenced:
            lines = lines[1:-1]
        if self.max_lines is not None and len(lines) > self.max_lines:
            # Bound wrapped content rows, including the omission marker, while
            # preserving the code block's padding and the final failure details.
            marker = Text("… earlier error output truncated", style="pcode.muted")
            marker.truncate(
                max(1, body_options.max_width - (2 if fenced else 0)), overflow="ellipsis"
            )
            if fenced:
                marker_lines = console.render_lines(
                    self._code_block(marker.plain), body_options, pad=False
                )[1:-1]
            else:
                marker_lines = console.render_lines(marker, body_options, pad=False)
            tail = lines[-(self.max_lines - 1) :] if self.max_lines > 1 else []
            lines = marker_lines + tail
        for line in top + lines + bottom:
            yield Segment(indent)
            yield from line
            yield Segment.line()
