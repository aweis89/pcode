"""Compose the terminal with either the offline preview or a real Coder agent."""

import argparse
import asyncio
import os
import shlex
import subprocess
import sys
from contextlib import aclosing
from pathlib import Path

from prompt_toolkit.application import get_app, in_terminal
from prompt_toolkit.input import create_input
from prompt_toolkit.styles import DynamicStyle
from rich.cells import cell_len
from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from pcode.commands import Command, CommandRegistry
from pcode.config import USAGE as CONFIG_USAGE
from pcode.config import config_arguments, configure
from pcode.preferences import apply_effort, effort_setting, load_preferences, save_preferences
from pcode.runtime import (
    Message,
    PlanPreview,
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
        self.tree_requested = False
        self.login_requested = False
        self.compact_requested: str | None = None
        self.mcp_enable_requested: str | None = None
        self.mcp_enabling: str | None = None
        self.model_requested = False
        self.registry = CommandRegistry()
        for command in (
            Command("/login", "Reuse pi's Anthropic login", self.login, ("pi",)),
            Command("/model", "Choose a model (Ctrl+L); keep the conversation", self.select_model),
            Command("/help", "Commands and keyboard shortcuts", self.help),
            Command("/tools", "Inspect tool calls and their results", self.tools, ("failed",)),
            Command("/errors", "Inspect failed tool calls", lambda _: self.tools("failed")),
            Command("/demo", "Sample Markdown, code, diff, and tool output", self.demo),
            Command(
                "/config",
                "Inspect or edit global defaults (next launch)",
                self.config,
                free_arguments=True,
                argument_provider=config_arguments,
            ),
            Command("/theme", "Switch palette: dark / light", self.theme, tuple(PALETTES)),
            Command("/colors", "Rich colors: palette / terminal", self.colors, COLOR_STYLES),
            Command(
                "/effort",
                "Reasoning effort: low / medium / high / xhigh / default",
                self.effort,
                ("low", "medium", "high", "xhigh", "default"),
            ),
            Command(
                "/mcp",
                "MCP servers: list / enable NAME / disable NAME (default off)",
                self.mcp,
                ("list", "enable", "disable"),
                free_arguments=True,
                argument_provider=self.mcp_arguments,
            ),
            Command(
                "/compact",
                "Summarize older context [optional focus]",
                self.compact,
                free_arguments=True,
            ),
            Command(
                "/autocompact",
                "Automatic LLM compaction: on / off (default off)",
                self.autocompact,
                ("on", "off"),
            ),
            Command("/context", "Model, workspace, and session usage", self.context),
            Command("/new", "Start a new saved conversation; keep transcript", self.new),
            Command("/tree", "Navigate and fork the conversation interactively", self.select_tree),
            Command("/session", "Choose a saved session to resume", self.select_session),
            Command("/sessions", "List saved sessions and resume instructions", self.sessions),
            Command("/quit", "Leave the terminal", self.quit, aliases=("/exit",)),
        ):
            self.registry.register(command)

    def compact(self, argument: str, *, before_queue: bool = False) -> None:
        if not self.model or not hasattr(self.runtime, "compact"):
            raise ValueError("/compact requires a live model session.")
        if not before_queue and (self.activity.busy or self.activity.queued_prompts):
            raise ValueError("/compact is unavailable while working. Cancel or wait, then retry.")
        self.compact_requested = argument

    def autocompact(self, argument: str) -> None:
        if not self.model or not hasattr(self.runtime, "auto_compact"):
            raise ValueError("/autocompact requires a live model session.")
        if argument:
            if self.activity.busy or self.activity.queued_prompts:
                raise ValueError("Change /autocompact while idle.")
            from pcode.compaction import effective_window

            window = effective_window(self.runtime.agent.model)
            if argument == "on" and window is None:
                raise ValueError(
                    "Unknown context window. Set PCODE_CONTEXT_WINDOW to the deployment's "
                    "token limit before enabling automatic compaction."
                )
            self.runtime.auto_compact = argument == "on"
            self.persist_defaults(autocompact=argument)
        state = "on" if self.runtime.auto_compact else "off"
        self.transcript.note(f"Automatic compaction: {state}. Usage: /autocompact on|off")

    def config(self, argument: str) -> None:
        try:
            result = configure(shlex.split(argument))
        except OSError as error:
            raise ValueError(f"Could not access global defaults: {error}") from None
        self.transcript.note(result)

    def persist_defaults(self, **updates: str) -> None:
        try:
            save_preferences(**updates)
        except (OSError, ValueError):
            self.transcript.warning("Could not save defaults; this selection applies only here.")

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
        await self.runtime.refresh_context()
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
            self.transcript.error(str(error))
        except Exception:
            self.transcript.error("Could not use pi login. No credential details were logged.")

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
        tree = getattr(self.runtime, "tree", None)
        if tree is not None:
            selected = set(tree.path(tree.active))
            archive.calls = [call for call in archive.calls if call.run_id in selected]
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

    def present_events(self, events) -> None:
        """Route live tool activity separately from permanent transcript writes."""
        for event in events:
            if isinstance(event, (ToolStarted, ToolSummary)):
                self.activity.tools.record(event)
                if self.transcript.output is not None:
                    self.transcript.output.app.invalidate()
                # Decide only after completion. The adapter's failed flag includes
                # non-zero shell exits, retries, and known tool validation failures.
                if isinstance(event, ToolSummary) and event.failed:
                    self.transcript.events((event,))
            else:
                self.transcript.events((event,))

    def demo(self, argument: str) -> None:
        self.present_events(self.preview.demo())

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
        effort = settings.get(effort_setting(self.model), "default")
        return "xhigh" if effort == "max" else effort

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
        if agent is None or effort_setting(self.model) is None:
            self.transcript.note(
                "Effort control requires an OpenAI/Codex, Anthropic, or Meridian model."
            )
            return
        # Replace rather than mutate: an active run keeps its captured settings.
        apply_effort(agent, self.model, value)
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
            self.transcript.note(
                "Automatic compaction: "
                + ("on" if getattr(self.runtime, "auto_compact", False) else "off")
                + " · /compact [focus] · /autocompact on|off"
            )
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

    def mcp_arguments(self) -> tuple[str, ...]:
        from pcode.mcp import configured_servers

        state = getattr(self.runtime, "mcp", None)
        enabled = state.enabled if state else {}
        try:
            names = configured_servers()
        except ValueError:
            names = {}
        return (
            "list",
            *(f"enable {name}" for name in sorted(names)),
            *(f"disable {name}" for name in sorted(enabled)),
        )

    def mcp(self, argument: str) -> None:
        from pcode.mcp import config_path, configured_servers

        parts = argument.split()
        state = getattr(self.runtime, "mcp", None)
        enabled = state.enabled if state else {}
        if not parts or parts == ["list"]:
            self.transcript.note(f"MCP config: {config_path()}")
            try:
                names = configured_servers()
            except ValueError as error:
                self.transcript.error(str(error))
                names = {}
            for name in sorted(names.keys() | enabled.keys()):
                status = "enabled" if name in enabled else "off"
                self.transcript.note(f"{name}: {status}")
            if not names and not enabled:
                self.transcript.note("No MCP servers configured. Add an mcpServers object here.")
            self.transcript.note("MCP defaults to off. Use /mcp enable NAME or /mcp disable NAME.")
            return
        if len(parts) != 2 or parts[0] not in {"enable", "disable"}:
            raise ValueError("Usage: /mcp list | /mcp enable NAME | /mcp disable NAME")
        # Slash commands precede queued (not yet running) prompts. In particular,
        # an enable + prompt submitted in one input batch must authenticate first.
        if self.mcp_enabling or (
            self.activity.busy
            and (self.activity.prompt_state == "running" or not self.activity.queued)
        ):
            raise ValueError("MCP cannot be changed while working. Cancel or wait, then retry.")
        if state is None:
            raise ValueError("MCP requires a live model. Start pcode with -m PROVIDER:MODEL.")
        action, name = parts
        if action == "enable":
            if name in enabled:
                self.transcript.note(f"MCP '{name}' is already enabled.")
            else:
                self.mcp_enable_requested = name
        else:
            state.disable(name)
            self.transcript.note(f"MCP '{name}' disabled. Earlier results remain in history.")

    async def enable_mcp(self, name: str) -> None:
        """Authorize outside the model loop; publish only a successfully enabled server."""
        await self.runtime.mcp.enable(name)
        self.transcript.note(
            f"MCP '{name}' enabled for this conversation. "
            "Its tools can perform actions with the server's permissions. "
            "OAuth tokens are kept in memory only."
        )

    def new(self, argument: str) -> None:
        self.runtime.reset()
        self.activity.reset()
        self.transcript.print(Rule("New conversation", style="pcode.muted"))
        self.transcript.note(
            "Context reset; MCP servers are off. Input history and transcript are unchanged."
        )
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
            await runtime.refresh_context()
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

    def select_tree(self, argument: str) -> None:
        if self.activity.busy or self.activity.queued:
            raise ValueError("/tree is unavailable while working or messages are queued.")
        self.tree_requested = True

    async def navigate_tree(self, identity: str | None, *, edit: bool = False) -> str:
        if self.activity.busy or self.activity.queued:
            raise ValueError("/tree is unavailable while working or messages are queued.")
        draft = await self.runtime.navigate(identity, edit=edit)
        self.activity.reset()
        self.transcript.print(Rule("Conversation branch", style="pcode.muted"))
        self.transcript.note(
            "Context switched; previous branches are kept. File changes and tool effects "
            "are not undone. Earlier scrollback is unchanged."
        )
        if self.runtime.session:
            self.replay()
        else:
            from pcode.diagnostics import redact

            tree = self.runtime.tree
            for node_id in tree.path(tree.active):
                node = tree.nodes[node_id]
                self.transcript.user(redact(node.prompt))
                if node.response:
                    self.transcript.events((Message(redact(node.response)),))
            self.activity.plan = tree.nodes[tree.active].plan if tree.active else []
        return draft

    async def choose_tree(self, output: TerminalOutput, session) -> None:
        from pcode.tree_ui import tree_dialog

        self.tree_requested = False
        tree = getattr(self.runtime, "tree", None)
        if tree is None or not tree.nodes:
            self.transcript.note("No conversation turns yet. Send a message to start a tree.")
            return
        await output.flush()
        async with output.lock:
            async with in_terminal():
                stdin = getattr(session.app.input, "stdin", None)
                modal_input = create_input(stdin=stdin) if stdin is not None else session.app.input
                try:
                    dialog = tree_dialog(
                        tree,
                        input=modal_input,
                        output=session.app.output,
                        style=session.app.style,
                    )
                    selection = await dialog.run_async()
                finally:
                    if modal_input is not session.app.input:
                        modal_input.close()
        if selection is not None:
            identity, edit = selection
            draft = await self.navigate_tree(identity, edit=edit)
            # A cancelled picker leaves the editor alone. Only user selection prefills it.
            if edit:
                session.default_buffer.text = draft
                session.default_buffer.cursor_position = len(draft)

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
                    self.transcript.warning("[Partial output from an interrupted run]")

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
                        parent_call_id=record.get("parent_call_id", ""),
                        activity=redact(record.get("activity", "")),
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
                        parent_call_id=record.get("parent_call_id", ""),
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
        model = self.model if self.model else "preview"
        details = plain(f"{model} · effort: {effort}", limit=None)
        context = ""
        if self.model:
            from pcode.context_usage import context_label

            resolved = getattr(getattr(self.runtime, "agent", None), "model", None)
            history = getattr(self.runtime, "context_history", None)
            if history is None:
                history = getattr(self.runtime, "history", ())
            context = context_label(resolved or self.model, history)
        if self.activity.busy:
            details += " · working"
            if self.activity.queued:
                details += f" · {self.activity.queued} queued"
        details += context
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
        segments.append(("text", context))
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
        if self.model and not text.startswith("/"):
            return True
        if text.startswith("/"):
            try:
                if not self.registry.dispatch(text):
                    self.transcript.warning(
                        "Unknown command. Type /help to see available commands."
                    )
            except ValueError as error:
                self.transcript.error(str(error))
        else:
            self.transcript.user(text)
            self.present_events(self.preview.reply(text))
        return False

    async def run_live(self, output: TerminalOutput, text: str) -> bool:
        from pcode.live import error_message

        output.begin_turn(text)
        self.activity.prompt = text
        self.activity.prompt_state = "running"
        self.activity.status = "Waiting for model…"

        def compaction_notice(text):
            self.activity.status = text
            self.transcript.note(text)

        self.runtime.compaction_notice = compaction_notice
        failure = None
        cancelled = False
        try:
            async with aclosing(self.runtime.stream(text)) as stream:
                async for event in stream:
                    if isinstance(event, TextDelta):
                        output.delta(event.text)
                        self.activity.status = "Responding…"
                    elif isinstance(event, RunStatus):
                        self.activity.status = event.text
                    elif isinstance(event, PlanUpdated):
                        self.activity.plan = event.items
                    elif isinstance(event, PlanPreview):
                        self.activity.plan_preview = event.items
                    elif isinstance(event, (ToolStarted, ToolSummary)):
                        # Only exceptional completions also become persistent output.
                        if isinstance(event, ToolSummary) and event.failed:
                            output.finish()
                        self.present_events((event,))
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
            self.activity.plan_preview = None
            output.end_turn()
            self.activity.tools.interrupt_running()
            self.activity.status = ""
        self.activity.prompt_state = "cancelled" if cancelled else "failed" if failure else "done"
        output.app.invalidate()
        if cancelled:
            self.transcript.cancelled()
        elif failure:
            self.transcript.error(error_message(failure), title="Agent failed")
        if (cancelled or failure) and self.runtime.session:
            self.transcript.note(f"Session and diagnostics: {self.runtime.session.directory}")
            if self.runtime.recovery_blocked:
                self.transcript.warning(self.runtime.recovery_blocked)
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
        refresh = getattr(self.runtime, "refresh_context", None)
        if refresh is not None:
            await refresh()
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
        mcp_task = None
        compact_task = None
        compact_idle = asyncio.Event()
        compact_idle.set()
        pending_compact = 0
        mcp_idle = asyncio.Event()
        mcp_idle.set()
        queue_generation = 0
        pending_mcp = 0

        def clear_queue():
            nonlocal queue_generation, pending_mcp, pending_compact
            queue_generation += 1
            if pending_mcp:
                self.transcript.warning("Pending MCP enable command cancelled.")
                pending_mcp = 0
            if pending_compact:
                self.transcript.warning("Pending compaction cancelled.")
                pending_compact = 0
            count = len(self.activity.queued_prompts)
            while not queue.empty():
                queue.get_nowait()
            self.activity.queued_prompts.clear()
            self.activity.queued = 0
            if count:
                self.transcript.note(f"Cleared {count} queued message(s).")

        def cancel():
            clear_queue()
            active = [
                task for task in (live_task, mcp_task, compact_task) if task and not task.done()
            ]
            if active:
                # Repeated interrupts must not interrupt persistence/auth cleanup.
                for task in active:
                    if not task.cancelling():
                        task.cancel()
            else:
                self.activity.busy = False
                self.transcript.cancelled()

        def submit(text):
            nonlocal pending_mcp, pending_compact
            text = text.strip()
            if text.startswith("/"):
                commands.put_nowait(
                    (
                        queue_generation,
                        text,
                        not self.activity.busy and not self.activity.queued_prompts,
                    )
                )
                command_idle.clear()
                if text.split()[0] == "/compact":
                    pending_compact += 1
                    self.activity.busy = True
                if text.split()[:2] == ["/mcp", "enable"]:
                    pending_mcp += 1
                    # Enter + Ctrl+C in one input batch must cancel activation
                    # before its command worker has had a chance to start OAuth.
                    self.activity.busy = True
            elif text:
                queue.put_nowait((queue_generation, text))
                self.activity.queued_prompts.append(text)
                self.activity.queued = len(self.activity.queued_prompts)
                # Set immediately so Enter + Ctrl+C in one input batch cancels
                # the pending request rather than clearing the user's draft.
                self.activity.busy = True

        def start_mcp_enable(name):
            nonlocal mcp_task
            mcp_idle.clear()
            self.mcp_enabling = name
            self.activity.busy = True
            self.activity.status = f"Enabling MCP '{name}' — complete browser sign-in if prompted…"
            self.transcript.note(
                f"Enabling MCP '{name}'. OAuth sign-in happens now if needed; "
                "Ctrl+C cancels. No model request is made."
            )

            def finished(task):
                nonlocal mcp_task
                success = False
                try:
                    task.result()
                    success = True
                except asyncio.CancelledError:
                    self.transcript.warning(f"MCP '{name}' sign-in cancelled; server remains off.")
                except Exception as error:
                    from pcode.live import error_message

                    self.transcript.error(f"MCP '{name}' remains off: {error_message(error)}")
                finally:
                    if not success:
                        clear_queue()
                    self.activity.busy = (
                        bool(self.activity.queued_prompts)
                        or bool(pending_mcp)
                        or bool(pending_compact)
                    )
                    self.activity.status = ""
                    mcp_task = None
                    self.mcp_enabling = None
                    mcp_idle.set()
                    session.app.invalidate()

            mcp_task = asyncio.create_task(self.enable_mcp(name))
            # A done callback also handles cancellation before the coroutine starts.
            mcp_task.add_done_callback(finished)

        def start_compact(focus):
            nonlocal compact_task
            compact_idle.clear()
            self.activity.busy = True
            self.activity.status = "Compacting context…"
            self.activity.prompt = "/compact" + (f" {focus}" if focus else "")
            self.activity.prompt_state = "running"
            self.transcript.note("Compacting context with the current model. Ctrl+C cancels.")

            def finished(task):
                nonlocal compact_task
                success = False
                try:
                    result = task.result()
                    self.transcript.note(result.description())
                    self.activity.prompt_state = "done"
                    success = True
                except asyncio.CancelledError:
                    self.activity.prompt_state = "cancelled"
                    self.transcript.warning("Compaction cancelled; history unchanged.")
                except Exception as error:
                    from pcode.live import error_message

                    self.activity.prompt_state = "failed"
                    self.transcript.error(error_message(error), title="Compaction failed")
                finally:
                    if not success:
                        clear_queue()
                    compact_task = None
                    self.activity.busy = (
                        bool(self.activity.queued_prompts)
                        or bool(pending_mcp)
                        or bool(pending_compact)
                    )
                    self.activity.status = ""
                    compact_idle.set()
                    session.app.invalidate()

            compact_task = asyncio.create_task(self.runtime.compact(focus))
            compact_task.add_done_callback(finished)

        async def consume_commands():
            nonlocal pending_mcp, pending_compact
            while self.running:
                generation, text, submitted_idle = await commands.get()
                try:
                    if text.split()[0] == "/compact":
                        if generation != queue_generation:
                            continue
                        pending_compact -= 1
                        self.activity.busy = bool(self.activity.queued_prompts) or any(
                            task is not None and not task.done()
                            for task in (live_task, mcp_task, compact_task)
                        )
                    if text.split()[:2] == ["/mcp", "enable"]:
                        if generation != queue_generation:
                            continue
                        pending_mcp -= 1
                        self.activity.busy = bool(self.activity.queued_prompts) or any(
                            task is not None and not task.done()
                            for task in (live_task, mcp_task, compact_task)
                        )
                    command = self.registry.find(text.split(maxsplit=1)[0])
                    before_queue = (
                        command is not None
                        and command.name == "/compact"
                        and submitted_idle
                        and not any(
                            task is not None and not task.done()
                            for task in (live_task, mcp_task, compact_task)
                        )
                    )
                    if (
                        command
                        and command.name
                        in {
                            "/new",
                            "/session",
                            "/tree",
                            "/login",
                            "/model",
                            "/compact",
                            "/autocompact",
                        }
                        and (self.activity.busy or self.activity.queued)
                        and not before_queue
                    ):
                        self.transcript.warning(
                            f"{command.name} is unavailable while working. "
                            "Cancel with Ctrl+C or wait for the run to finish, then retry."
                        )
                    else:
                        if before_queue:
                            parts = text.split(maxsplit=1)
                            self.compact(parts[1] if len(parts) > 1 else "", before_queue=True)
                        else:
                            self.handle(text)
                        if self.compact_requested is not None:
                            focus = self.compact_requested
                            self.compact_requested = None
                            start_compact(focus)
                        if self.mcp_enable_requested is not None:
                            name = self.mcp_enable_requested
                            self.mcp_enable_requested = None
                            start_mcp_enable(name)
                        if not self.running:
                            cancel()
                            active = [
                                task
                                for task in (live_task, mcp_task, compact_task)
                                if task is not None
                            ]
                            if active:
                                await asyncio.gather(*active, return_exceptions=True)
                        if self.model_requested:
                            await self.choose_model(output, session)
                        if self.login_requested:
                            await self.login_pi()
                        if self.tree_requested:
                            await self.choose_tree(output, session)
                        if self.session_requested:
                            await self.choose_session(output, session)
                        if self.inspector_requested is not None:
                            await self.inspect_tools(output, session)
                except Exception as error:
                    from pcode.live import error_message

                    self.transcript.error(error_message(error))
                finally:
                    if pending_mcp or pending_compact:
                        self.activity.busy = True
                    if commands.empty():
                        command_idle.set()
                await output.flush()
                if not self.running and session.app.is_running:
                    session.app.exit()

        async def consume():
            nonlocal live_task
            while self.running:
                await command_idle.wait()
                await mcp_idle.wait()
                await compact_idle.wait()
                generation, text = await queue.get()
                await command_idle.wait()
                await mcp_idle.wait()
                await compact_idle.wait()
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
                            self.transcript.cancelled()
                except Exception as error:
                    from pcode.live import error_message

                    self.transcript.error(error_message(error), title="Agent failed")
                    success = False
                finally:
                    live_task = None
                if not session.app.is_running:
                    return
                if not success:
                    clear_queue()
                self.activity.busy = (
                    bool(self.activity.queued_prompts) or bool(pending_mcp) or bool(pending_compact)
                )
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
            if compact_task is not None:
                if not compact_task.done() and not compact_task.cancelling():
                    compact_task.cancel()
                await asyncio.gather(compact_task, return_exceptions=True)
            if mcp_task is not None:
                if not mcp_task.done() and not mcp_task.cancelling():
                    mcp_task.cancel()
                await asyncio.gather(mcp_task, return_exceptions=True)
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
    subparsers = parser.add_subparsers(dest="command")
    config_parser = subparsers.add_parser(
        "config",
        help="Inspect or edit global defaults without starting a session",
        description=f"Global defaults. Usage: pcode {CONFIG_USAGE}",
    )
    config_parser.add_argument("arguments", nargs="*", metavar="ARG")
    args = parser.parse_args()
    if args.command == "config":
        try:
            print(configure(args.arguments))
        except (OSError, ValueError) as error:
            parser.exit(2, f"{error}\n")
        return
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
