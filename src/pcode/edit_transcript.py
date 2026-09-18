"""Literal, width-aware edit blocks, independent of assistant Markdown."""

from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, RenderResult
from rich.rule import Rule
from rich.segment import Segment
from rich.syntax import Syntax
from rich.text import Text

from pcode.edits import edit_text
from pcode.runtime import EditCompleted


@dataclass(frozen=True)
class EditTranscript:
    change: EditCompleted
    code_theme: str = "monokai"
    max_rows: int = 60

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        change = self.change
        yield Rule(style="pcode.muted")
        yield Text(
            f"✓ {change.operation.capitalize()} {edit_text(change.path)}"
            f" · +{change.added} −{change.removed}",
            style="pcode.accent",
        )
        if change.patch:
            patch = Syntax(
                edit_text(change.patch),
                "diff",
                theme=self.code_theme,
                word_wrap=True,
                background_color="default",
            )
            rows = console.render_lines(patch, options, pad=False)
            for row in rows[: self.max_rows]:
                yield from row
                yield Segment.line()
            if len(rows) > self.max_rows or change.truncated:
                yield Text("… additional diff rows omitted", style="pcode.muted")
        if change.omitted:
            yield Text(f"Diff unavailable: {edit_text(change.omitted)}", style="pcode.muted")
        yield Rule(style="pcode.muted")
