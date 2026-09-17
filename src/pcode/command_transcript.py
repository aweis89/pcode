"""Compact command executions, independent of assistant Markdown rendering."""

from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, RenderResult
from rich.rule import Rule
from rich.segment import Segment
from rich.syntax import Syntax
from rich.text import Text


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
        yield Rule(style="pcode.muted")
        elapsed = f" · {self.elapsed_seconds:.1f}s" if self.elapsed_seconds is not None else ""
        status = "✗" if self.failed else "✓"
        title = f"{self.title} failed" if self.failed else self.title
        yield Text(
            f"{status} {title}{elapsed}",
            style="pcode.error" if self.failed else "pcode.accent",
        )
        indent = "  " if options.max_width > 2 else ""
        body_options = options.update(width=max(1, options.max_width - len(indent)))
        if self.shell_command:
            command = Syntax(
                self.command,
                "bash",
                theme=self.code_theme,
                word_wrap=True,
                background_color="default",
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
        yield Rule(style="pcode.muted")
