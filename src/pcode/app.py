"""Composition root for the deliberately offline UI preview."""

import argparse
import sys

from prompt_toolkit.application import get_app
from rich.console import Console

from pcode.commands import Command, CommandRegistry
from pcode.runtime import PreviewRuntime
from pcode.ui import PALETTES, Transcript, create_prompt


class PreviewApp:
    def __init__(self, theme: str = "dark", console: Console | None = None) -> None:
        self.runtime = PreviewRuntime()
        self.transcript = Transcript(console or Console(), theme)
        self.running = True
        self.registry = CommandRegistry()
        for command in (
            Command("/help", "Commands and keyboard shortcuts", self.help),
            Command("/demo", "Sample Markdown, code, diff, and tool output", self.demo),
            Command("/theme", "Switch palette: dark / light", self.theme, tuple(PALETTES)),
            Command("/context", "What is real in this preview", self.context),
            Command("/new", "Reset the demo counter; keep scrollback", self.new),
            Command("/quit", "Leave the preview", self.quit, aliases=("/exit",)),
        ):
            self.registry.register(command)

    def help(self, argument: str) -> None:
        self.transcript.help(self.registry)

    def demo(self, argument: str) -> None:
        self.transcript.events(self.runtime.demo())

    def theme(self, argument: str) -> None:
        self.transcript.theme = argument or ("light" if self.transcript.theme == "dark" else "dark")
        self.transcript.note(f"Theme: {self.transcript.theme}. Existing output is unchanged.")

    def context(self, argument: str) -> None:
        self.transcript.note(
            f"Preview turns: {self.runtime.turns} · model: none · tools: none · network: none"
        )
        self.transcript.note(
            "Canned replies only. No persistence, streaming, or agent execution yet."
        )

    def new(self, argument: str) -> None:
        self.runtime.reset()
        self.transcript.console.rule("New preview", style=self.transcript.palette.muted)
        self.transcript.note("Counter reset. Input history and terminal scrollback are unchanged.")

    def quit(self, argument: str) -> None:
        self.running = False

    def toolbar(self):
        width = get_app().output.get_size().columns
        if width < 60:
            text = " preview · /help · Ctrl+D exit"
        else:
            text = " preview · Enter send · Alt+Enter newline · / commands · Ctrl+D exit"
        return [("class:bottom-toolbar.text", text)]

    def handle(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.transcript.user(text)
        if text.startswith("/"):
            try:
                if not self.registry.dispatch(text):
                    self.transcript.note("Unknown command. Type /help to see available commands.")
            except ValueError as error:
                self.transcript.note(str(error))
        else:
            self.transcript.events(self.runtime.reply(text))

    def run(self) -> None:
        self.transcript.welcome()
        session = create_prompt(self.registry, bottom_toolbar=self.toolbar)
        while self.running:
            try:
                text = session.prompt(style=self.transcript.palette.prompt_style())
                self.handle(text)
            except KeyboardInterrupt:
                self.transcript.note("Input discarded. Ctrl+D on an empty prompt exits.")
            except EOFError:
                break
        self.transcript.note("Goodbye. Your transcript stays in terminal scrollback.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline, scrollback-native terminal UI preview")
    parser.add_argument("--theme", choices=PALETTES, default="dark")
    parser.add_argument(
        "--demo", action="store_true", help="Print a sample and exit (no TTY needed)"
    )
    args = parser.parse_args()
    app = PreviewApp(theme=args.theme)
    if args.demo:
        app.transcript.welcome()
        app.demo("")
    elif not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("interactive mode needs a terminal; use --demo for a non-interactive sample")
    else:
        app.run()


if __name__ == "__main__":
    main()
