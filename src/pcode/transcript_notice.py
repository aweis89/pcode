"""Notices and diagnostics for terminal scrollback."""

import re
from dataclasses import dataclass
from typing import Literal

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Markdown
from rich.segment import Segment
from rich.text import Text

# What marks a line as pcode's own rather than the model's. A different shade
# alone is not enough: a three-line `/mcp` listing between two replies reads as
# another paragraph of prose. The dot gives a note a left edge prose never
# has, in the same vocabulary as the prompt rail and tool markers.
NOTE_MARK = "\u00b7"


@dataclass(frozen=True)
class Note:
    """An informational message from pcode: one mark, then a hanging indent.

    Only the first row carries the mark. A note may wrap or span lines (a
    listing with indented rows, a long URL), and marking each row would break
    the listing's own indentation and put a dot in the middle of the URL. The
    hanging indent keeps every row under the text, so the whole note reads as
    one item.
    """

    text: str

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if not self.text:
            yield Segment.line()
            return
        mark = console.get_style("pcode.accent", default="none")
        style = console.get_style("pcode.note", default="none")
        prefix = f"{NOTE_MARK} " if options.max_width > 2 else ""
        lines = console.render_lines(
            Text(self.text, style=style),
            options.update(width=max(1, options.max_width - len(prefix))),
            pad=False,
        )
        for index, line in enumerate(lines):
            yield Segment(prefix, mark) if index == 0 else Segment(" " * len(prefix))
            yield from line
            yield Segment.line()


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
        # Logged code-block backgrounds start at the terminal edge; keep Rich
        # padding and the log's own indentation inside the block unchanged.
        logged = self.kind == "error"
        indent = "  " if not logged and options.max_width > 2 else ""
        body_options = options.update(width=max(1, options.max_width - len(indent)))
        # Rich's Markdown code blocks have one padding row/column on each side.
        # Fall back to literal text only when the pane cannot fit that padding.
        fenced = logged and body_options.max_width > 2
        body = self._code_block(self.text) if fenced else Text(self.text)
        lines = console.render_lines(body, body_options, pad=False)
        top, bottom = (lines[:1], lines[-1:]) if fenced else ([], [])
        if fenced:
            lines = lines[1:-1]
        if self.max_lines is not None and len(lines) > self.max_lines:
            # Bound wrapped content rows, including the omission marker, while
            # preserving the code block's padding and the final failure details.
            kind = "error " if self.kind == "error" else ""
            marker = Text(f"… earlier {kind}output truncated", style="pcode.muted")
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
