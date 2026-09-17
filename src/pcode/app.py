"""Compose the terminal with either the offline preview or a real Coder agent."""

import argparse
import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path

from prompt_toolkit.application import get_app, in_terminal
from prompt_toolkit.input import create_input
from prompt_toolkit.styles import DynamicStyle
from rich.cells import cell_len
from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from pcode.commands import Command, CommandRegistry
from pcode.preferences import apply_effort, load_preferences, save_preferences
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
from pcode.ui import COLOR_STYLES, PALETTES, Activity, TerminalOutput, Transcript, create_prompt


class PreviewApp:
    def __init__(
        self,
        theme: str | None = None,
        console: Console | None = None,
        *,
        color_style: str = "palette",
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
        self.save_sessions = save or saved_session is not None
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
        agent = getattr(self.runtime, "agent", None)
        if agent is not None and model:
            apply_effort(agent, model, load_preferences().get("effort"))
        self.activity = Activity()
        self.transcript = Transcript(
            console or Console(),
            theme or load_preferences().get("theme", "dark"),
            activity=self.activity,
            color_style=color_style,
        )
        self.running = True
        self.inspector_requested: str | None = None
        self.session_requested = False
        self.login_requested = False
        self.model_requested = False
        self.registry = CommandRegistry()
        for command in (
            Command("/login", "Reuse pi's Anthropic login", self.login, ("pi",)),
            Command("/model", "Choose a model (Ctrl+L); keep the conversation", self.select_model),
            Command("/help", "Commands and keyboard shortcuts", self.help),
            Command("/tools", "Inspect tool calls and their results", self.tools, ("failed",)),
            Command("/errors", "Inspect failed tool calls", lambda _: self.tools("failed")),
            Command("/demo", "Sample Markdown, code, diff, and tool output", self.demo),
            Command("/theme", "Switch palette: dark / light", self.theme, tuple(PALETTES)),
            Command("/colors", "Rich colors: palette / terminal", self.colors, COLOR_STYLES),
            Command(
                "/effort",
                "Reasoning effort: low / medium / high / xhigh / default",
                self.effort,
                ("low", "medium", "high", "xhigh", "default"),
            ),
            Command("/context", "Model, workspace, and session usage", self.context),
            Command("/new", "Start a new saved conversation; keep transcript", self.new),
            Command("/session", "Choose a saved session to resume", self.select_session),
            Command("/sessions", "List saved sessions and resume instructions", self.sessions),
            Command("/quit", "Leave the terminal", self.quit, aliases=("/exit",)),
        ):
            self.registry.register(command)

    def persist_defaults(self, **updates: str) -> None:
        try:
            save_preferences(**updates)
        except OSError:
            self.transcript.note("Could not save defaults; this selection applies only here.")

    def select_model(self, argument: str) -> None:
        self.model_requested = True

    async def switch_model(self, model: str) -> None:
        from pcode.agent import create_agent
        from pcode.live import AgentRuntime
        from pcode.sessions import SavedSession

        if self.activity.busy or self.activity.queued_prompts:
            raise ValueError("Cannot change models while working or messages are queued.")
        if model == self.model:
            self.persist_defaults(model=model)
            self.transcript.note(f"Already using {model}.")
            return
        # Construct first: a missing provider/login must leave the old session intact.
        agent = await asyncio.to_thread(create_agent, model, self.workspace)
        apply_effort(agent, model, load_preferences().get("effort"))
        save = self.save_sessions or getattr(self.runtime, "session_factory", None) is not None
        root = self.session_dir
        workspace = self.workspace
        factory = (lambda: SavedSession.create(model, workspace, root)) if save else None
        if isinstance(self.runtime, AgentRuntime):
            saved = self.runtime.session
            if saved is not None:
                previous_model = saved.info.model
                saved.info.model = model
                try:
                    saved.save_info()
                except OSError:
                    saved.info.model = previous_model
                    raise
            self.runtime.replace_agent(agent)
            self.runtime.session_factory = factory
        else:
            self.runtime = AgentRuntime(agent, session_factory=factory)
        self.model = model
        self.persist_defaults(model=model)
        self.save_sessions = save
        self.transcript.note(f"Model: {model}. Continuing the current conversation.")
        self.show_startup_context()

    async def choose_model(self, output: TerminalOutput, session) -> None:
        from pcode.model_ui import ModelPicker
        from pcode.models import active_providers, model_catalog

        self.model_requested = False
        providers = await asyncio.to_thread(active_providers, self.model)
        if not providers:
            self.transcript.note(
                "No active model providers. Use /login for pi Anthropic auth, "
                "set ANTHROPIC_API_KEY, or run codex login."
            )
            return
        values = model_catalog(providers, self.model)
        await output.flush()
        async with output.lock:
            async with in_terminal():
                stdin = getattr(session.app.input, "stdin", None)
                modal_input = create_input(stdin=stdin) if stdin is not None else session.app.input
                try:
                    picker = ModelPicker(
                        values,
                        providers,
                        current=self.model,
                        input=modal_input,
                        output=session.app.output,
                        style=session.app.style,
                    )
                    model = await picker.run()
                finally:
                    if modal_input is not session.app.input:
                        modal_input.close()
        if model is not None:
            await self.switch_model(model)

    def login(self, argument: str) -> None:
        if self.model and not self.model.startswith("anthropic:"):
            self.transcript.note("/login currently supports Anthropic only.")
            return
        self.login_requested = True

    async def login_pi(self) -> None:
        self.login_requested = False
        from pcode.auth import LoginError
        from pcode.pi_auth import PiAnthropicModel, pi_auth_path, read_pi_credential

        try:
            if self.model:
                model = await asyncio.to_thread(PiAnthropicModel, self.model)
                self.runtime.agent.model = model
            else:
                await asyncio.to_thread(read_pi_credential, pi_auth_path())
            os.environ["PCODE_ANTHROPIC_AUTH"] = "pi"
            self.transcript.note(
                "Using pi's Anthropic credential (read-only). "
                "Refresh/login in pi when it expires; pcode never writes pi's auth file."
            )
            self.transcript.note(
                "For future launches use PCODE_ANTHROPIC_AUTH=pi pcode -m anthropic:<model-id>."
            )
        except LoginError as error:
            self.transcript.note(str(error))
        except Exception:
            self.transcript.note("Could not use pi login. No credential details were logged.")

    def tools(self, argument: str) -> None:
        self.inspector_requested = argument

    async def inspect_tools(self, output: TerminalOutput, session) -> None:
        from pcode.inspection import ToolArchive
        from pcode.inspector_ui import ToolInspector

        failed = self.inspector_requested == "failed"
        self.inspector_requested = None
        saved = getattr(self.runtime, "session", None)
        if saved is not None or hasattr(self.runtime, "inspections"):
            # The browser owns a snapshot, never the archive streaming mutates.
            # File indexing can then run off-thread, including saved history.
            from copy import deepcopy

            archive = deepcopy(getattr(self.runtime, "inspections", None) or ToolArchive())
            if saved is not None:
                await asyncio.to_thread(archive.update, saved.directory / "transcript.jsonl")
        else:
            archive = ToolArchive()
            for call in self.activity.tools.calls:
                archive.event(call.event)
            archive.settle("unknown")
        await output.flush()
        # One terminal owner: drain permanent output, suspend the editor, and
        # hold the writer lock until the alternate screen has been restored.
        async with output.lock:
            async with in_terminal():
                # The suspended editor can still have an escape-flush timer.
                # Give the modal its own parser, or that timer can steal an
                # early Escape from the shared input object's parser buffer.
                stdin = getattr(session.app.input, "stdin", None)
                modal_input = create_input(stdin=stdin) if stdin is not None else session.app.input
                try:
                    inspector = ToolInspector(
                        archive,
                        failed=failed,
                        input=modal_input,
                        output=session.app.output,
                        style=session.app.style,
                    )
                    await inspector.run()
                finally:
                    if modal_input is not session.app.input:
                        modal_input.close()

    def help(self, argument: str) -> None:
        self.transcript.help(self.registry)

    def demo(self, argument: str) -> None:
        self.transcript.events(self.preview.demo())

    def theme(self, argument: str) -> None:
        self.transcript.theme = argument or ("light" if self.transcript.theme == "dark" else "dark")
        self.persist_defaults(theme=self.transcript.theme)
        self.transcript.note(f"Theme: {self.transcript.theme}. Existing output is unchanged.")

    def colors(self, argument: str) -> None:
        if argument:
            self.transcript.color_style = argument
        self.transcript.note(
            f"Colors: {self.transcript.color_style}. Existing output is unchanged."
        )

    def current_effort(self) -> str:
        if not self.model:
            return "n/a"
        agent = getattr(self.runtime, "agent", None)
        settings = getattr(getattr(agent, "model", None), "settings", None) or {}
        settings = {**settings, **(getattr(agent, "model_settings", None) or {})}
        return settings.get("openai_reasoning_effort", "default")

    def effort(self, argument: str) -> None:
        value = argument.strip().lower()
        if not value:
            self.transcript.note(
                f"Effort: {self.current_effort()}. Usage: /effort low|medium|high|xhigh|default"
            )
            return
        if value not in ("low", "medium", "high", "xhigh", "default"):
            self.transcript.note("Usage: /effort low|medium|high|xhigh|default")
            return
        agent = getattr(self.runtime, "agent", None)
        provider = (self.model or "").split(":", 1)[0]
        if agent is None or provider not in (
            "openai",
            "openai-chat",
            "openai-responses",
            "openai-codex",
        ):
            self.transcript.note("Effort control requires an OpenAI/Codex model.")
            return
        # Replace rather than mutate: an active run keeps its captured settings.
        settings = dict(agent.model_settings or {})
        if value == "default":
            settings.pop("openai_reasoning_effort", None)
        else:
            settings["openai_reasoning_effort"] = value
        agent.model_settings = settings
        self.persist_defaults(model=self.model, effort=value)
        self.transcript.note(f"Effort: {self.current_effort()} (next turn).")

    def adjust_effort(self, direction: int) -> None:
        levels = ("low", "medium", "high", "xhigh")
        current = self.current_effort()
        # The provider default is unspecified; use medium as the starting point.
        index = levels.index(current) if current in levels else 1
        self.effort(levels[max(0, min(len(levels) - 1, index + direction))])

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
        self.activity.reset()
        self.transcript.print(Rule("New conversation", style="pcode.muted"))
        self.transcript.note("Context reset. Input history and transcript are unchanged.")
        if self.model and self.runtime.session:
            self.transcript.note(f"Saving session: {self.runtime.session.info.id}")

    def select_session(self, argument: str) -> None:
        self.session_requested = True

    async def resume_session(self, identity: str) -> None:
        from pcode.agent import create_agent
        from pcode.live import AgentRuntime
        from pcode.sessions import SavedSession, SessionError

        current = getattr(self.runtime, "session", None)
        if current is not None and current.info.id == identity:
            self.transcript.note("This session is already active.")
            return
        saved = SavedSession.open(identity, self.session_dir)
        try:
            if Path(saved.info.workspace).resolve() != self.workspace:
                raise SessionError("Workspace differs; refusing cross-repo resume.")
            agent = create_agent(saved.info.model, self.workspace)
            apply_effort(agent, saved.info.model, load_preferences().get("effort"))
            runtime = AgentRuntime(agent, saved)
            await runtime.restore()
        except BaseException:
            saved.close()
            raise
        # Keep the current conversation intact until recovery has succeeded.
        close = getattr(self.runtime, "close", None)
        if close is not None:
            close()
        self.runtime = runtime
        self.model = saved.info.model
        self.session_dir = saved.directory.parent
        self.activity.prompt = ""
        self.replay()

    async def choose_session(self, output: TerminalOutput, session) -> None:
        from pcode.diagnostics import redact
        from pcode.session_ui import session_dialog
        from pcode.sessions import first_prompt, list_sessions

        self.session_requested = False
        records = [
            info
            for info in list_sessions(self.session_dir)
            if Path(info.workspace).resolve() == self.workspace
        ]
        if not records:
            self.transcript.note("No saved sessions for this workspace.")
            return
        current = getattr(self.runtime, "session", None)
        values = [
            (
                info.id,
                f"{plain(redact(first_prompt(info, self.session_dir)), 100)}"
                f"\n  {info.updated[:16]} · {plain(info.model)} · {info.id[:8]}"
                + (" · active" if current and current.info.id == info.id else ""),
            )
            for info in records
        ]
        await output.flush()
        async with output.lock:
            async with in_terminal():
                stdin = getattr(session.app.input, "stdin", None)
                modal_input = create_input(stdin=stdin) if stdin is not None else session.app.input
                try:
                    dialog = session_dialog(
                        values,
                        input=modal_input,
                        output=session.app.output,
                        style=session.app.style,
                    )
                    identity = await dialog.run_async()
                finally:
                    if modal_input is not session.app.input:
                        modal_input.close()
        if identity is not None:
            await self.resume_session(identity)

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
        effort = self.current_effort()
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
        segments = [("text", " ")]
        if path.plain:
            segments.extend([("location", path.plain), ("text", " · ")])
        model_text = plain(model, limit=None)
        segments.extend(
            [("model", model_text), ("text", plain(f" · effort: {effort}", limit=None))]
        )
        if self.activity.busy:
            segments.extend([("text", " · "), ("activity", "working")])
            if self.activity.queued:
                segments.extend([("text", " · "), ("activity", f"{self.activity.queued} queued")])
        # Slice the already cell-truncated text, preserving its ellipsis and the
        # same narrow-terminal priorities without splitting wide characters.
        fragments = []
        remaining = text.plain
        for role, value in segments:
            if not remaining:
                break
            fragments.append((f"class:bottom-toolbar.{role}", remaining[: len(value)]))
            remaining = remaining[len(value) :]
        return fragments

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
        commands = asyncio.Queue()
        command_idle = asyncio.Event()
        command_idle.set()
        live_task = None
        queue_generation = 0

        def clear_queue():
            nonlocal queue_generation
            queue_generation += 1
            count = len(self.activity.queued_prompts)
            while not queue.empty():
                queue.get_nowait()
            self.activity.queued_prompts.clear()
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
            text = text.strip()
            if text.startswith("/"):
                commands.put_nowait(text)
                command_idle.clear()
            elif text:
                queue.put_nowait((queue_generation, text))
                self.activity.queued_prompts.append(text)
                self.activity.queued = len(self.activity.queued_prompts)
                # Set immediately so Enter + Ctrl+C in one input batch cancels
                # the pending request rather than clearing the user's draft.
                self.activity.busy = True

        async def consume_commands():
            while self.running:
                text = await commands.get()
                try:
                    command = self.registry.find(text.split(maxsplit=1)[0])
                    if (
                        command
                        and command.name in {"/new", "/session", "/login", "/model"}
                        and self.activity.busy
                    ):
                        self.transcript.user(text)
                        self.transcript.note(
                            f"{command.name} is unavailable while working. "
                            "Cancel with Ctrl+C or wait for the run to finish, then retry."
                        )
                    else:
                        self.handle(text)
                        if not self.running:
                            cancel()
                            if live_task is not None:
                                await live_task
                        if self.model_requested:
                            await self.choose_model(output, session)
                        if self.login_requested:
                            await self.login_pi()
                        if self.session_requested:
                            await self.choose_session(output, session)
                        if self.inspector_requested is not None:
                            await self.inspect_tools(output, session)
                except Exception as error:
                    from pcode.live import error_message

                    self.transcript.note(error_message(error))
                finally:
                    if commands.empty():
                        command_idle.set()
                await output.flush()
                if not self.running and session.app.is_running:
                    session.app.exit()

        async def consume():
            nonlocal live_task
            while self.running:
                await command_idle.wait()
                generation, text = await queue.get()
                await command_idle.wait()
                if not self.running:
                    return
                if generation != queue_generation:
                    continue  # Cancelled while waiting for a command/modal.
                self.activity.queued_prompts.pop(0)
                self.activity.queued = len(self.activity.queued_prompts)
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
                self.activity.busy = bool(self.activity.queued_prompts)
                await output.flush()
                if not self.running:
                    session.app.exit()

        session = create_prompt(
            self.registry,
            activity=self.activity,
            transcript=self.transcript,
            on_submit=submit,
            on_cancel=cancel,
            on_effort=self.adjust_effort,
            on_model=lambda: submit("/model"),
            bottom_toolbar=self.toolbar,
        )
        output = TerminalOutput(
            self.transcript.console,
            session.app,
            code_theme=lambda: self.transcript.code_theme,
            rich_theme=lambda: self.transcript.rich_theme,
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
            session.app.create_background_task(consume_commands())

        try:
            await session.app.run_async(pre_run=start)
        finally:
            await output.flush()
            self.transcript.output = None
        saved = getattr(self.runtime, "session", None)
        if saved is None:
            self.transcript.console.print("Session not saved; no resume command available.")
        else:
            from pcode.sessions import session_root

            command = ["pcode", "--resume", saved.info.id]
            if saved.directory.parent.resolve() != session_root().resolve():
                command.extend(["--session-dir", str(saved.directory.parent.resolve())])
            self.transcript.console.print(f"Resume with: {shlex.join(command)}", markup=False)

    def run(self) -> None:
        asyncio.run(self.run_async())


def main() -> None:
    parser = argparse.ArgumentParser(description="Streaming terminal with a Coder agent")
    parser.add_argument(
        "--theme", choices=PALETTES, default=load_preferences().get("theme", "dark")
    )
    parser.add_argument(
        "--color-style",
        choices=COLOR_STYLES,
        default="palette",
        help="Rich output colors (default: palette; terminal uses ANSI colors)",
    )
    parser.add_argument(
        "-m",
        "--model",
        help="Pydantic Agent model string; omitted = saved default or offline preview",
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
        console = Transcript(Console(), args.theme, color_style=args.color_style)
        records = list_sessions(args.session_dir)
        if not records:
            console.note("No saved sessions.")
        for info in records:
            console.note(f"{info.id}  {info.status}  {info.model}  {info.workspace}")
        return
    if args.demo:
        # --demo never constructs a provider, even when -m is also supplied.
        app = PreviewApp(theme=args.theme, color_style=args.color_style)
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
        if not args.resume and not args.model:
            args.model = load_preferences().get("model")
        workspace = args.workspace or Path.cwd()
        if not workspace.is_dir():
            raise SessionError("Workspace must be an existing directory.")
        app = PreviewApp(
            theme=args.theme,
            color_style=args.color_style,
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
        if app is not None and app.model:
            app.runtime.close()
        if saved is not None:
            saved.close()


if __name__ == "__main__":
    main()
