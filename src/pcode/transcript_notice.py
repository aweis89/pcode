"""Notices and diagnostics for terminal scrollback."""

import re
from dataclasses import dataclass
from typing import Literal

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.text import Text

from pcode import glyphs
from pcode.markdown import Markdown

# What marks a line as pcode's own rather than the model's. A different shade
# alone is not enough: a three-line `/mcp` listing between two replies reads as
# another paragraph of prose. The dot gives a note a left edge prose never
# has, in the same vocabulary as the prompt rail and tool markers.
NOTE_MARK = "\u00b7"

# A warning's left edge. Thinner than the `▌` rail on the user's own prompts,
# so the two never read as the same thing even before the colour registers.
# `glyphs.to_ascii` already maps it to `|`.
WARNING_BAR = "\u258e"

# The headline ends at the first sentence break or line break. Only punctuation
# followed by space ends a sentence, which keeps `a.py` and `v1.2` whole, and
# not before a lowercase word, which keeps `e.g. haiku` and `-m 'fix. it'` whole.
_HEADLINE = re.compile(r"(.*?)(?:[ \t\r]*\n|(?<=[.!?])[ \t]+(?![a-z]))(.*)", re.DOTALL)
# A closing period after a word is punctuation; `..`, `…` or a bare `.` path is
# part of the message.
_FULL_STOP = re.compile(r"(?<=[\w)\]'\"`])\.$")


def split_headline(text: str) -> tuple[str, str]:
    """Split a warning into its first sentence and the rest.

    Warnings arrive as one string from dozens of call sites (and over the
    session-host socket), and almost all of them already lead with what
    happened and follow with why or what to do. Splitting here gives every
    one of them a headline without a second argument threaded through.
    """
    match = _HEADLINE.fullmatch(text.strip())
    if match is None or not match.group(1).strip():
        headline, detail = text.strip(), ""
    else:
        headline, detail = match.group(1), match.group(2)
    return _FULL_STOP.sub("", headline), detail


@dataclass(frozen=True)
class WarningNotice:
    """A warning: a coloured bar on every row, headline first, details muted.

    The bar holds a wrapped warning together as one block; the colour on the
    headline alone says where to look, while the explanation recedes. No
    "Warning" title row: the bar's colour already says it.
    """

    text: str

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        bar = console.get_style("pcode.warning", default="none")
        muted = console.get_style("pcode.muted", default="none")
        headline, detail = split_headline(self.text)
        # Styled as spans, not as the Text's base style, which would also
        # embolden the detail.
        body = Text()
        body.append(headline or "Warning", style=bar)
        if detail:
            body.append("\n")
            body.append(detail, style=muted)
        prefix = f"{WARNING_BAR} " if options.max_width > 2 else ""
        lines = console.render_lines(
            body, options.update(width=max(1, options.max_width - len(prefix))), pad=False
        )
        for line in lines:
            yield Segment(prefix, bar)
            yield from line
            yield Segment.line()


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
    kind: Literal["error", "cancelled"]
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
        symbol = glyphs.icon("fail") if self.kind == "error" else glyphs.icon("blocked")
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
