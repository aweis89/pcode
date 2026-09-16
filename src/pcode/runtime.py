"""Application events and the offline fixture runtime; no terminal imports."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    markdown: str


@dataclass(frozen=True)
class ToolSummary:
    name: str
    detail: str
    failed: bool = False


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class RunStatus:
    text: str


Event = Message | ToolSummary | TextDelta | RunStatus


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
                "The greeting now handles an empty name:\n\n"
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
                "- Use PageUp/PageDown to scroll and Ctrl+End to follow new output.\n\n"
                "**No files were read or changed, and no tests were executed.**"
            ),
        )
