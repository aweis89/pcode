"""Muted Markdown for readable thinking with normal block spacing."""

from dataclasses import dataclass

from rich.markdown import Markdown
from rich.segment import Segment


@dataclass
class ThinkingMarkdown:
    source: str
    code_theme: str = "monokai"
    style: str = "pcode.thinking"

    def __rich_console__(self, console, options):
        # Codex also emits heading-only Markdown summaries:
        # https://github.com/openai/codex/issues/34873
        markdown = Markdown(self.source, code_theme=self.code_theme)
        # Keep provider line boundaries rather than folding adjacent thoughts
        # into a single paragraph. Markdown's other inline/block rules still apply.
        for token in markdown.parsed:
            for child in token.children or ():
                if child.type == "softbreak":
                    child.type = "hardbreak"
        style = console.get_style(self.style)
        # Keep one spacer row between blocks, including streamed commits. Rich
        # also pads lists/code with blank rows; don't double those on replay.
        pending_blank = False
        rendered = False
        for line in Segment.split_lines(console.render(markdown, options)):
            if not any(segment.text.strip() for segment in line):
                pending_blank = rendered
                continue
            if pending_blank:
                yield Segment.line()
            yield from Segment.apply_style(line, post_style=style)
            yield Segment.line()
            rendered = True
            pending_blank = False
        if rendered:
            yield Segment.line()
