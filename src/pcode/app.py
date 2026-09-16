"""Compose the terminal with either the offline preview or a real Coder agent."""

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from prompt_toolkit.application import get_app
from prompt_toolkit.styles import DynamicStyle
from rich.cells import cell_len
from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from pcode.commands import Command, CommandRegistry
from pcode.runtime import (
    Message,
    PlanUpdated,
    PreviewRuntime,
    RunStatus,
    TextDelta,
    ToolStarted,
    ToolSummary,
)
from pcode.tool_display import plain
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
        self.branch = ""
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
        self.transcript = Transcript(console or Console(), theme, activity=self.activity)
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
        self.activity.plan = []
        self.activity.tools.clear()
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
        self.activity.plan = saved.latest_plan()
        self.transcript.note(f"Resumed {saved.info.id}; showing recent transcript.")
        for record in saved.recent_transcript():
            kind = record["kind"]
            if kind == "turn_started":
                self.transcript.user(redact(record["prompt"]))
            elif kind in ("Message", "partial"):
                self.transcript.events((Message(redact(record["markdown"])),))
                if kind == "partial":
                    self.transcript.note("[Partial output from an interrupted run]")

        # Tool history is independent of the bounded conversation replay. Replay
        # lifecycle events so concurrency order and interrupted starts survive.
        self.activity.tools.clear()
        for record in saved.tool_events():
            kind = record["kind"]
            if kind.startswith("turn_"):
                self.activity.tools.interrupt_running()
            elif kind == "ToolStarted":
                self.activity.tools.record(
                    ToolStarted(
                        record["name"],
                        redact(record["detail"]),
                        record["call_id"],
                        redact(record.get("command", "")),
                    )
                )
            else:
                self.activity.tools.record(
                    ToolSummary(
                        record["name"],
                        redact(record["detail"]),
                        record.get("failed", False),
                        record.get("call_id", ""),
                        record.get("elapsed_seconds"),
                        redact(record.get("error", "")),
                        redact(record.get("command", "")),
                    )
                )
        self.activity.tools.interrupt_running()

    def quit(self, argument: str) -> None:
        self.running = False

    def refresh_branch(self) -> None:
        """Read only Git metadata; called off the UI thread, never during rendering."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.workspace), "symbolic-ref", "--quiet", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=1,
            )
            if result.returncode == 1:  # Detached HEAD: show its short commit instead.
                result = subprocess.run(
                    ["git", "-C", str(self.workspace), "rev-parse", "--short", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
            self.branch = plain(result.stdout.strip(), limit=None) if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            self.branch = ""

    def toolbar(self):
        width = get_app().output.get_size().columns
        try:
            relative = self.workspace.relative_to(Path.home())
            directory = "~" if relative == Path(".") else f"~/{relative}"
        except ValueError:
            directory = str(self.workspace)
        location = plain(directory, limit=None)
        if self.branch:
            location += f" {self.branch}"
        agent = getattr(self.runtime, "agent", None)
        settings = getattr(getattr(agent, "model", None), "settings", None) or {}
        settings = {**settings, **(getattr(agent, "model_settings", None) or {})}
        effort = settings.get("openai_reasoning_effort", "default") if self.model else "n/a"
        model = self.model.split(":", 1)[-1] if self.model else "preview"
        details = plain(f"{model} · effort: {effort}", limit=None)
        if self.activity.busy:
            details += " · working"
            if self.activity.queued:
                details += f" · {self.activity.queued} queued"
        # Keep the model/effort visible before spending space on a long path.
        path_width = max(0, width - cell_len(details) - 4)
        path = Text(location if path_width else "")
        if path_width:
            path.truncate(path_width, overflow="ellipsis")
        text = Text(f" {path.plain} · {details}" if path.plain else f" {details}")
        text.truncate(width, overflow="ellipsis")
        return [("class:bottom-toolbar.text", text.plain)]

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

        self.activity.prompt = text
        self.activity.prompt_state = "running"
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
                elif isinstance(event, PlanUpdated):
                    self.activity.plan = event.items
                elif isinstance(event, (ToolStarted, ToolSummary)):
                    # Tool activity is mutable UI state, not a model-text boundary.
                    self.transcript.events((event,))
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
            self.activity.tools.interrupt_running()
            self.activity.status = ""
        self.activity.prompt_state = "cancelled" if cancelled else "failed" if failure else "done"
        output.app.invalidate()
        if cancelled:
            self.transcript.note("Run cancelled. Completed tool effects are not undone.")
        elif failure:
            self.transcript.note(error_message(failure))
        if (cancelled or failure) and self.runtime.session:
            self.transcript.note(f"Session and diagnostics: {self.runtime.session.directory}")
            if self.runtime.recovery_blocked:
                self.transcript.note(self.runtime.recovery_blocked)
        return not (cancelled or failure)

    def show_startup_context(self) -> None:
        summary = getattr(self.runtime, "startup_context", None)
        if summary is not None:
            for line in summary():
                self.transcript.note(line)

    async def run_async(self) -> None:
        # This frontend owns the terminal; suppress the framework's unsolicited banner.
        os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
        if self.resuming:
            await self.runtime.restore()
        self.transcript.welcome(self.model, str(self.workspace))
        self.show_startup_context()
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
                        self.activity.prompt = text
                        self.activity.prompt_state = "running"
                        live_task = asyncio.create_task(self.run_live(output, text))
                        try:
                            success = await live_task
                        except asyncio.CancelledError:
                            # Cancellation before run_live's first instruction.
                            if not session.app.is_running:
                                return
                            success = False
                            self.activity.prompt_state = "cancelled"
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
        output = TerminalOutput(
            self.transcript.console,
            self.activity,
            session.app,
            code_theme=lambda: self.transcript.palette.syntax,
        )
        self.transcript.output = output
        session.app.style = DynamicStyle(lambda: self.transcript.palette.prompt_style())

        async def watch_branch():
            while True:
                await asyncio.to_thread(self.refresh_branch)
                session.app.invalidate()
                await asyncio.sleep(2)

        def start():
            session.app.create_background_task(watch_branch())
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
        # There is no mutable panel in the non-interactive sample.
        app.transcript.events(app.preview.demo(), show_tools=True)
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
