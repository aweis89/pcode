"""One block style for a run of output, shared by scrollback and the live panel.

A command, an edit or a live preview is drawn the same way everywhere: a top
line that carries the heading, a two-space indented body, and a closing line.
Scrollback draws it with Rich and the live panel with prompt_toolkit windows,
so the containers cannot be one object; the heading text, the indent and the
rule character live here so the two surfaces cannot drift apart.
"""

from rich.rule import Rule
from rich.text import Text

RULE = "─"
INDENT = "  "
# Heading markers. A non-zero exit is routine, so the marker alone reports it:
# a word or an alarm colour would make every expected failure look like a crash.
RUNNING = "⟳"
DONE = "✓"
FAILED = "✗"


def block_heading(icon: str, title: str, elapsed_seconds: float | None = None) -> str:
    """The text that sits on the opening line: marker, what ran, how long."""
    elapsed = f" · {elapsed_seconds:.1f}s" if elapsed_seconds is not None else ""
    return f"{icon} {title}{elapsed}"


def block_rule(heading: str | None = None) -> Rule:
    """The opening line when given a heading, the closing line when not."""
    if heading is None:
        return Rule(characters=RULE, style="pcode.muted")
    # Left-aligned so the heading reads as a label on the line rather than as a
    # centered banner; Rich fills the remainder of the row with the rule.
    return Rule(
        Text(heading, style="pcode.accent"),
        characters=RULE,
        style="pcode.muted",
        align="left",
    )
