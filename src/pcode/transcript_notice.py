"""Notices and diagnostics for terminal scrollback."""

import re
from dataclasses import dataclass
from typing import ClassVar

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.text import Text

from pcode import glyphs

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
    # The bar and headline's style; an error is the same block in red.
    style: ClassVar[str] = "pcode.warning"
    fallback: ClassVar[str] = "Warning"

    def parts(self) -> tuple[str, str]:
        return split_headline(self.text)

    def bound(self, console: Console, options: ConsoleOptions, lines: list) -> list:
        return lines

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        bar = console.get_style(self.style, default="none")
        muted = console.get_style("pcode.muted", default="none")
        headline, detail = self.parts()
        prefix = f"{WARNING_BAR} " if options.max_width > 2 else ""
        inner = options.update(width=max(1, options.max_width - len(prefix)))
        lines = console.render_lines(Text(headline or self.fallback, style=bar), inner, pad=False)
        if detail:
            body = console.render_lines(Text(detail, style=muted), inner, pad=False)
            lines += self.bound(console, inner, body)
        for line in lines:
            yield Segment(prefix, bar)
            yield from line
            yield Segment.line()


@dataclass(frozen=True)
class ErrorNotice(WarningNotice):
    """An error: the warning's bar block, in red.

    A plain error leads with its own first sentence, like a warning. A titled
    one (`Agent failed`, `Run shell failed`) leads with the title and keeps the
    whole message as detail. Long detail (a traceback, a command's stderr)
    keeps its last `max_lines` rows, where the cause usually is.
    """

    title: str = "Error"
    max_lines: int | None = None
    style: ClassVar[str] = "pcode.error"
    fallback: ClassVar[str] = "Error"

    def parts(self) -> tuple[str, str]:
        if self.title == "Error":
            return super().parts()
        return self.title, self.text.strip("\n")

    def bound(self, console: Console, options: ConsoleOptions, lines: list) -> list:
        if self.max_lines is None or len(lines) <= self.max_lines:
            return lines
        marker = Text("… earlier error output truncated", style="pcode.muted")
        marker.truncate(options.max_width, overflow="ellipsis")
        tail = lines[-(self.max_lines - 1) :] if self.max_lines > 1 else []
        return console.render_lines(marker, options, pad=False) + tail


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
class CancelledNotice:
    """The one-line mark left where a cancelled run stopped."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield Text(f"{glyphs.icon('blocked')} Run cancelled", style="pcode.warning")
