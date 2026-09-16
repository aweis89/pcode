"""Compose the terminal with either the offline preview or a real Coder agent."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from prompt_toolkit.application import get_app
from prompt_toolkit.styles import DynamicStyle
from rich.console import Console
from rich.rule import Rule

from pcode.commands import Command, CommandRegistry
from pcode.runtime import Message, PreviewRuntime, RunStatus, TextDelta, ToolSummary
from pcode.ui import PALETTES, Activity, TerminalOutput, Transcript, create_prompt


class PreviewApp:
    def __init__(
        self,
        theme: str = "dark",
        console: Console | None = None,
        *,
        model: str | None = None,
        workspace: Path | None = None,
        runtime=None,
        saved_session=None,
        save: bool = False,
        session_dir: Path | None = None,
        resume: bool = False,
    ) -> None:
        self.model = model
        self.resuming = resume
        self.workspace = (workspace or Path.cwd()).resolve()
        self.session_dir = saved_session.directory.parent if saved_session else session_dir
        self.preview = PreviewRuntime()
        self.runtime = runtime or self.preview
        if model and runtime is None:
            from pcode.agent import create_agent
            from pcode.live import AgentRuntime
            from pcode.sessions import SavedSession

            self.runtime = AgentRuntime(
                create_agent(model, self.workspace),
                saved_session,
                session_factory=(
                    (lambda: SavedSession.create(model, self.workspace, session_dir))
                    if save
                    else None
                ),
            )
        self.activity = Activity()
        self.transcript = Transcript(console or Console(), theme)
        self.running = True
        self.registry = CommandRegistry()
        for command in (
            Command("/help", "Commands and keyboard shortcuts", self.help),
            Command("/demo", "Sample Markdown, code, diff, and tool output", self.demo),
            Command("/theme", "Switch palette: dark / light", self.theme, tuple(PALETTES)),
            Command("/context", "Model, workspace, and session usage", self.context),
            Command("/new", "Start a new saved conversation; keep transcript", self.new),
            Command("/sessions", "List saved sessions and resume instructions", self.sessions),
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
            self.transcript.note("Coder tools enabled; no sandbox.")
            if self.runtime.session:
                self.transcript.note(f"Session: {self.runtime.session.info.id}")
                self.transcript.note(f"Saved in: {self.runtime.session.directory}")
            elif self.runtime.session_factory is not None:
                self.transcript.note("Session will be saved after your first prompt.")
            else:
                self.transcript.note("Saving disabled; session is in memory only.")
        else:
            self.transcript.note(
                f"Preview turns: {self.runtime.turns} · model: none · tools: none · network: none"
            )
            self.transcript.note(
                "Canned replies only. Start with -m PROVIDER:MODEL for a real agent."
            )

    def new(self, argument: str) -> None:
        self.runtime.reset()
        self.transcript.print(Rule("New conversation", style=self.transcript.palette.muted))
        self.transcript.note("Context reset. Input history and transcript are unchanged.")
        if self.model and self.runtime.session:
            self.transcript.note(f"Saving session: {self.runtime.session.info.id}")

    def sessions(self, argument: str) -> None:
        from pcode.sessions import list_sessions

        saved = getattr(self.runtime, "session", None)
        root = saved.directory.parent if saved else self.session_dir
        records = list_sessions(root)
        if not records:
            self.transcript.note("No saved sessions.")
        for info in records[:20]:
            self.transcript.note(f"{info.id}  {info.status}  {info.model}  {info.workspace}")
        self.transcript.note("Restart with: pcode --resume SESSION_ID (or --resume latest)")

    def replay(self) -> None:
        from pcode.diagnostics import redact

        saved = self.runtime.session
        self.transcript.note(f"Resumed {saved.info.id}; showing recent transcript.")
        for record in saved.recent_transcript():
            kind = record["kind"]
            if kind == "turn_started":
                self.transcript.user(redact(record["prompt"]))
            elif kind in ("Message", "partial"):
                self.transcript.events((Message(redact(record["markdown"])),))
                if kind == "partial":
                    self.transcript.note("[Partial output from an interrupted run]")
            elif kind == "ToolSummary":
                self.transcript.events(
                    (ToolSummary(record["name"], record["detail"], record.get("failed", False)),)
                )
            elif kind in ("turn_failed", "turn_cancelled"):
                self.transcript.note(f"[{kind.replace('_', ' ')}; diagnostics saved]")
        if saved.info.status != "complete":
            self.transcript.note("Recovered the last settled checkpoint. No tools were replayed.")

    def quit(self, argument: str) -> None:
        self.running = False

    def toolbar(self):
        width = get_app().output.get_size().columns
        if self.activity.busy:
            queued = f" · {self.activity.queued} queued" if self.activity.queued else ""
            text = f" working · Ctrl+C cancel · Enter queues{queued}"
            if width >= 100 and self.activity.status:
                text += f" · {self.activity.status}"
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

    async def run_live(self, output: TerminalOutput, text: str) -> bool:
        from pcode.live import error_message

        self.activity.status = "Waiting for model…"
        failure = None
        cancelled = False
        try:
            async for event in self.runtime.stream(text):
                if isinstance(event, TextDelta):
                    output.delta(event.text)
                    self.activity.status = "Responding…"
                elif isinstance(event, RunStatus):
                    self.activity.status = event.text
                elif isinstance(event, Message):
                    output.finish(event.markdown)
                else:
                    output.finish()
                    self.transcript.events((event,))
                output.app.invalidate()
        except asyncio.CancelledError:
            cancelled = True
        except Exception as error:
            failure = error
        finally:
            output.finish()
            self.activity.status = ""
        if cancelled:
            self.transcript.note("Run cancelled. Completed tool effects are not undone.")
        elif failure:
            self.transcript.note(error_message(failure))
        if (cancelled or failure) and self.runtime.session:
            self.transcript.note(f"Session and diagnostics: {self.runtime.session.directory}")
            if self.runtime.recovery_blocked:
                self.transcript.note(self.runtime.recovery_blocked)
        return not (cancelled or failure)

    async def run_async(self) -> None:
        # This frontend owns the terminal; suppress the framework's unsolicited banner.
        os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
        if self.resuming:
            await self.runtime.restore()
        self.transcript.welcome(self.model, str(self.workspace))
        if self.model and self.runtime.session:
            self.transcript.note(f"Saving session: {self.runtime.session.info.id}")
        if self.resuming:
            self.replay()
        queue = asyncio.Queue()
        live_task = None

        def clear_queue():
            count = queue.qsize()
            while not queue.empty():
                queue.get_nowait()
            self.activity.queued = 0
            if count:
                self.transcript.note(f"Cleared {count} queued message(s).")

        def cancel():
            clear_queue()
            if live_task is not None and not live_task.done():
                # Repeated interrupts must not interrupt persistence/cleanup.
                if not live_task.cancelling():
                    live_task.cancel()
            else:
                self.activity.busy = False
                self.transcript.note("Run cancelled. Completed tool effects are not undone.")

        def submit(text):
            if text.strip():
                queue.put_nowait(text.strip())
                self.activity.queued = queue.qsize()
                # Set immediately so Enter + Ctrl+C in one input batch cancels
                # the pending request rather than clearing the user's draft.
                self.activity.busy = True

        async def consume():
            nonlocal live_task
            while self.running:
                text = await queue.get()
                self.activity.queued = queue.qsize()
                success = True
                try:
                    if self.handle(text):
                        live_task = asyncio.create_task(self.run_live(output, text))
                        try:
                            success = await live_task
                        except asyncio.CancelledError:
                            # Cancellation before run_live's first instruction.
                            if not session.app.is_running:
                                return
                            success = False
                            self.transcript.note(
                                "Run cancelled. Completed tool effects are not undone."
                            )
                except Exception as error:
                    from pcode.live import error_message

                    self.transcript.note(error_message(error))
                    success = False
                finally:
                    live_task = None
                if not session.app.is_running:
                    return
                if not success:
                    clear_queue()
                self.activity.busy = not queue.empty()
                await output.flush()
                if not self.running:
                    session.app.exit()

        session = create_prompt(
            self.registry,
            activity=self.activity,
            transcript=self.transcript,
            on_submit=submit,
            on_cancel=cancel,
            bottom_toolbar=self.toolbar,
        )
        output = TerminalOutput(self.transcript.console, self.activity, session.app)
        self.transcript.output = output
        session.app.style = DynamicStyle(lambda: self.transcript.palette.prompt_style())

        def start():
            session.app.create_background_task(output.run())
            session.app.create_background_task(consume())

        try:
            await session.app.run_async(pre_run=start)
        finally:
            await output.flush()
            self.transcript.output = None
        self.transcript.console.print("Goodbye.")

    def run(self) -> None:
        asyncio.run(self.run_async())


def main() -> None:
    parser = argparse.ArgumentParser(description="Streaming terminal with a Coder agent")
    parser.add_argument("--theme", choices=PALETTES, default="dark")
    parser.add_argument(
        "-m", "--model", help="Pydantic Agent model string; omitted = offline preview"
    )
    parser.add_argument(
        "-C", "--workspace", type=Path, help="Coder workspace (default: current directory)"
    )
    parser.add_argument("--demo", action="store_true", help="Print an offline sample and exit")
    parser.add_argument("--sessions", action="store_true", help="List saved sessions and exit")
    parser.add_argument("--resume", nargs="?", const="latest", help="Resume ID/prefix, or latest")
    parser.add_argument(
        "--session-dir", type=Path, help="Override the private session storage directory"
    )
    parser.add_argument(
        "--no-save", action="store_true", help="Keep this live session in memory only"
    )
    args = parser.parse_args()
    if args.resume and (args.no_save or args.demo):
        parser.error("--resume cannot be combined with --no-save or --demo")
    from pcode.sessions import SavedSession, SessionError, list_sessions

    if args.sessions:
        console = Transcript(Console(), args.theme)
        records = list_sessions(args.session_dir)
        if not records:
            console.note("No saved sessions.")
        for info in records:
            console.note(f"{info.id}  {info.status}  {info.model}  {info.workspace}")
        return
    if args.demo:
        # --demo never constructs a provider, even when -m is also supplied.
        app = PreviewApp(theme=args.theme)
        app.transcript.welcome()
        app.demo("")
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("interactive mode needs a terminal; use --demo for a non-interactive sample")
    saved = None
    app = None
    try:
        if args.resume:
            saved = SavedSession.open(args.resume, args.session_dir)
            if args.model and args.model != saved.info.model:
                raise SessionError(
                    "Cannot change models when resuming; start a new session instead."
                )
            if args.workspace and str(args.workspace.resolve()) != saved.info.workspace:
                raise SessionError(
                    "Workspace differs from the saved session; refusing cross-repo resume."
                )
            args.model = saved.info.model
            args.workspace = Path(saved.info.workspace)
        workspace = args.workspace or Path.cwd()
        if not workspace.is_dir():
            raise SessionError("Workspace must be an existing directory.")
        app = PreviewApp(
            theme=args.theme,
            model=args.model,
            workspace=workspace,
            saved_session=saved,
            save=not args.no_save,
            session_dir=args.session_dir,
            resume=bool(args.resume),
        )
        app.run()
    except Exception as error:
        from pcode.live import error_message

        parser.exit(2, error_message(error) + "\n")
    finally:
        if app is not None and args.model:
            app.runtime.close()
        if saved is not None:
            saved.close()


if __name__ == "__main__":
    main()
