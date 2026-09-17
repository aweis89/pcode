"""Application events and the offline fixture runtime; no terminal imports."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    markdown: str


@dataclass(frozen=True)
class ToolStarted:
    name: str
    detail: str
    call_id: str
    command: str = ""
    arguments: str | None = None
    run_id: str = ""
    started_at: str = ""
    process_id: str = ""


@dataclass(frozen=True)
class ToolSummary:
    name: str
    detail: str
    failed: bool = False
    call_id: str = ""
    elapsed_seconds: float | None = None
    error: str = ""
    command: str = ""
    result: str | None = None
    run_id: str = ""
    outcome: str = ""
    process_id: str = ""


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class RunStatus:
    text: str


@dataclass(frozen=True)
class PlanUpdated:
    """Authoritative Harness plan snapshot, not a parsed tool summary."""

    items: list[dict]


Event = Message | ToolStarted | ToolSummary | TextDelta | RunStatus | PlanUpdated


class PreviewRuntime:
    def __init__(self) -> None:
        self.turns = 0

    def reset(self) -> None:
        self.turns = 0

    def reply(self, prompt: str) -> tuple[Event, ...]:
        self.turns += 1
        return (
            Message(
                "This is a **local UI preview**, not a connected model. "
                f"Your {len(prompt.splitlines())}-line message arrived intact.\n\n"
                "Try `/demo` for a sample coding response, `/theme light` to change the palette, "
                "or type `/` to browse commands. Nothing is sent anywhere."
            ),
        )

    def demo(self) -> tuple[Event, ...]:
        self.turns += 1
        return (
            Message("Here’s a **sample coding response**. Tool activity below is fictional."),
            ToolSummary("Read", "example/greeting.py · 8 lines · preview only"),
            ToolSummary("Edit", "example/greeting.py · +2 −1 · preview only"),
            Message(
                "## Terminal-native colors\n\n"
                "The greeting now handles an empty name with `greet(name)`. "
                "Here is **bold**, *italic*, and a "
                "[Rich documentation link](https://rich.readthedocs.io/).\n\n"
                "> Quotes and inline code use your terminal colors, "
                "without a painted background.\n\n"
                "```python\n"
                "def greet(name: str) -> str:\n"
                '    name = name.strip() or "world"\n'
                '    return f"Hello, {name}!"\n'
                "```\n\n"
                "```diff\n"
                '-    return f"Hello, {name}!"\n'
                '+    name = name.strip() or "world"\n'
                '+    return f"Hello, {name}!"\n'
                "```\n\n"
                "| Input | Result |\n"
                "| --- | --- |\n"
                "| `Ada` | Hello, Ada! |\n"
                "| empty | Hello, world! |\n"
                "| `世界` | Hello, 世界! |\n\n"
                "- Markdown, code, and this table reflow when the terminal is resized.\n"
                "- Try `/theme light` or `/theme dark`, "
                "then `/demo` again to compare syntax colors.\n"
                "- Use terminal/tmux scrollback to compare previous output.\n\n"
                "**No files were read or changed, and no tests were executed.**"
            ),
        )
