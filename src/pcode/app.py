"""Compose the terminal with either the offline preview or a real Coder agent."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from prompt_toolkit.application import get_app, run_in_terminal
from rich.console import Console

from pcode.commands import Command, CommandRegistry
from pcode.runtime import Message, PreviewRuntime, RunStatus, TextDelta
from pcode.ui import PALETTES, Activity, Transcript, create_prompt


class PreviewApp:
    def __init__(
        self,
        theme: str = "dark",
        console: Console | None = None,
        *,
        model: str | None = None,
        workspace: Path | None = None,
        runtime=None,
    ) -> None:
        self.model = model
        self.workspace = (workspace or Path.cwd()).resolve()
        self.preview = PreviewRuntime()
        self.runtime = runtime or self.preview
        if model and runtime is None:
            from pcode.agent import create_agent
            from pcode.live import AgentRuntime

            self.runtime = AgentRuntime(create_agent(model, self.workspace))
        self.activity = Activity()
        self.transcript = Transcript(console or Console(), theme)
        self.running = True
        self.registry = CommandRegistry()
        for command in (
            Command("/help", "Commands and keyboard shortcuts", self.help),
            Command("/demo", "Sample Markdown, code, diff, and tool output", self.demo),
            Command("/theme", "Switch palette: dark / light", self.theme, tuple(PALETTES)),
            Command("/context", "Model, workspace, and session usage", self.context),
            Command("/new", "Reset the conversation; keep scrollback", self.new),
            Command("/quit", "Leave the terminal", self.quit, aliases=("/exit",)),
        ):
            self.registry.register(command)

    def help(self, argument: str) -> None:
        self.transcript.help(self.registry)

    def demo(self, argument: str) -> None:
        self.transcript.events(self.preview.demo())

    def theme(self, argument: str) -> None:
        self.transcript.theme = argument or ("light" if self.transcript.theme == "dark" else "dark")
        self.transcript.note(f"Theme: {self.transcript.theme}. Existing output is unchanged.")

    def context(self, argument: str) -> None:
        if self.model:
            self.transcript.note(f"Model: {self.model} · workspace: {self.workspace}")
            self.transcript.note(
                f"Turns: {self.runtime.turns} · tokens in/out: "
                f"{self.runtime.input_tokens}/{self.runtime.output_tokens}"
            )
            self.transcript.note("Coder tools enabled. Session is in memory only; no sandbox.")
        else:
            self.transcript.note(
                f"Preview turns: {self.runtime.turns} · model: none · tools: none · network: none"
            )
            self.transcript.note(
                "Canned replies only. Start with -m PROVIDER:MODEL for a real agent."
            )

    def new(self, argument: str) -> None:
        self.runtime.reset()
        self.transcript.console.rule("New conversation", style=self.transcript.palette.muted)
        self.transcript.note("Context reset. Input history and terminal scrollback are unchanged.")

    def quit(self, argument: str) -> None:
        self.running = False

    def toolbar(self):
        width = get_app().output.get_size().columns
        if self.activity.busy:
            text = " working · Ctrl+C cancel"
        elif width < 60:
            text = f" {'coder' if self.model else 'preview'} · /help · Ctrl+D exit"
        else:
            mode = "coder" if self.model else "preview"
            text = f" {mode} · Enter send · Alt+Enter newline · / commands · Ctrl+D exit"
        return [("class:bottom-toolbar.text", text)]

    def handle(self, text: str) -> bool:
        """Handle commands/preview synchronously; return whether a live run is needed."""
        text = text.strip()
        if not text:
            return False
        self.transcript.user(text)
        if text.startswith("/"):
            try:
                if not self.registry.dispatch(text):
                    self.transcript.note("Unknown command. Type /help to see available commands.")
            except ValueError as error:
                self.transcript.note(str(error))
        elif self.model:
            return True
        else:
            self.transcript.events(self.preview.reply(text))
        return False

    async def run_live(self, session, text: str) -> None:
        from pcode.live import error_message

        activity = self.activity
        activity.busy = True
        activity.text = ""
        activity.status = "Waiting for model…"
        failure = None
        cancelled = False

        async def produce() -> None:
            nonlocal failure
            try:
                async for event in self.runtime.stream(text):
                    if isinstance(event, TextDelta):
                        activity.text += event.text
                        activity.status = "Responding…"
                    elif isinstance(event, RunStatus):
                        activity.status = event.text
                    else:
                        if isinstance(event, Message):
                            activity.text = ""
                        # Suspend only the mutable prompt, then commit the finished
                        # block normally to stdout. Never repaint old transcript.
                        await run_in_terminal(lambda event=event: self.transcript.events((event,)))
                    session.app.invalidate()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failure = error
            finally:
                if not session.app.is_done:
                    session.app.exit(result="")

        try:
            await session.prompt_async(
                style=self.transcript.palette.prompt_style(),
                pre_run=lambda: session.app.create_background_task(produce()),
            )
        except (KeyboardInterrupt, EOFError):
            cancelled = True
        finally:
            # PromptSession waits for its background task cancellation/cleanup.
            activity.busy = False
        if activity.text:
            self.transcript.events((Message(activity.text),))
            activity.text = ""
        if cancelled:
            self.transcript.note("Run cancelled. Completed tool effects are not undone.")
        elif failure:
            self.transcript.note(error_message(failure))
        activity.status = ""

    async def run_async(self) -> None:
        # This frontend owns the terminal; suppress the framework's unsolicited banner.
        os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
        self.transcript.welcome(self.model, str(self.workspace))
        session = create_prompt(self.registry, activity=self.activity, bottom_toolbar=self.toolbar)
        while self.running:
            try:
                text = await session.prompt_async(style=self.transcript.palette.prompt_style())
                if self.handle(text):
                    await self.run_live(session, text.strip())
            except KeyboardInterrupt:
                self.transcript.note("Input discarded. Ctrl+D on an empty prompt exits.")
            except EOFError:
                break
        self.transcript.note("Goodbye. Your transcript stays in terminal scrollback.")

    def run(self) -> None:
        asyncio.run(self.run_async())


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrollback-native terminal with a Coder agent")
    parser.add_argument("--theme", choices=PALETTES, default="dark")
    parser.add_argument(
        "-m", "--model", help="Pydantic Agent model string; omitted = offline preview"
    )
    parser.add_argument("-C", "--workspace", type=Path, default=Path.cwd(), help="Coder workspace")
    parser.add_argument("--demo", action="store_true", help="Print an offline sample and exit")
    args = parser.parse_args()
    if not args.workspace.is_dir():
        parser.error("workspace must be an existing directory")
    if args.demo:
        # --demo never constructs a provider, even when -m is also supplied.
        app = PreviewApp(theme=args.theme)
        app.transcript.welcome()
        app.demo("")
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("interactive mode needs a terminal; use --demo for a non-interactive sample")
    try:
        app = PreviewApp(theme=args.theme, model=args.model, workspace=args.workspace)
    except Exception as error:
        from pcode.live import error_message

        parser.exit(2, error_message(error) + "\n")
    app.run()


if __name__ == "__main__":
    main()
