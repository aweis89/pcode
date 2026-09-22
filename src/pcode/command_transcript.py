"""Compact command executions, independent of assistant Markdown rendering."""

from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.syntax import Syntax
from rich.text import Text

from pcode.block import DONE, FAILED, INDENT, block_heading, block_rule
from pcode.syntax import transparent_theme


@dataclass(frozen=True)
class CommandTranscript:
    command: str
    output: str
    title: str
    failed: bool = False
    elapsed_seconds: float | None = None
    max_lines: int | None = None
    code_theme: str = "monokai"
    shell_command: bool = True

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        # The heading rides the opening line; the mirrored output carries the
        # detail behind a non-zero exit.
        yield block_rule(
            block_heading(FAILED if self.failed else DONE, self.title, self.elapsed_seconds)
        )
        indent = INDENT if options.max_width > len(INDENT) else ""
        body_options = options.update(width=max(1, options.max_width - len(indent)))
        if self.shell_command:
            command = Syntax(
                self.command,
                "bash",
                theme=transparent_theme(self.code_theme),
                word_wrap=True,
            ).highlight(self.command)
            # Pygments adds a final newline; the render loop supplies its own.
            command.rstrip()
            invocation = Text("$ ", style="pcode.muted")
            invocation.append_text(command)
        else:
            # Process polling details aren't shell source or executable commands.
            invocation = Text(self.command, style="pcode.muted")
        for line in console.render_lines(invocation, body_options, pad=False):
            yield Segment(indent)
            yield from line
            yield Segment.line()

        lines = console.render_lines(Text(self.output), body_options, pad=False)
        if self.max_lines is not None and len(lines) > self.max_lines:
            retained = max(0, self.max_lines)
            omitted = len(lines) - retained
            marker = Text(f"… {omitted} earlier output rows omitted", style="pcode.muted")
            marker.truncate(body_options.max_width, overflow="ellipsis")
            marker_lines = console.render_lines(marker, body_options, pad=False)
            lines = marker_lines + (lines[-retained:] if retained else [])
        for line in lines:
            yield Segment(indent)
            yield from line
            yield Segment.line()
        yield block_rule()
