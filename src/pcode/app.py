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
from pcode.preferences import (
    apply_effort,
    apply_thinking,
    effort_setting,
    load_preferences,
    save_preferences,
)
from pcode.runtime import (
    CommandOutput,
    EditCompleted,
    EditPreview,
    Message,
    PlanPreview,
    PlanUpdated,
    PreviewRuntime,
    RunStatus,
    TextDelta,
    Thinking,
    ThinkingDelta,
    ToolStarted,
    ToolSummary,
)
from pcode.theme import THEMES
from pcode.tool_display import COMMAND_TOOLS, plain
from pcode.ui import (
    COLOR_STYLES,
    SYSTEM_COMMAND_LABELS,
    Activity,
    TerminalOutput,
    Transcript,
    create_prompt,
)


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
        self.send_mode = load_preferences().get("send_mode", "steering")
        self.model = model
        self.resuming = resume
        self.save_sessions = save or saved_session is not None
        self.workspace = (workspace or Path.cwd()).resolve()
        self.branch = ""
        self.session_dir = saved_session.directory.parent if saved_session else session_dir
        self.preview = PreviewRuntime()
        self.runtime = runtime or self.preview
        self._saved_session = saved_session
        self._needs_runtime = bool(model and runtime is None)
        self._startup_pending = self._needs_runtime or resume
        self._startup_error: Exception | None = None
        agent = getattr(self.runtime, "agent", None)
        if agent is not None and model:
            apply_effort(agent, model, load_preferences().get("effort"))
        self.activity = Activity(
            show_tasks=load_preferences().get("show_tasks", "on") == "on",
            autohide_tasks=load_preferences().get("autohide_tasks", "on") == "on",
            show_thinking=load_preferences().get("show_thinking") == "on",
        )
        if agent is not None and model:
            apply_thinking(agent, model, self.activity.show_thinking)
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
        self.login_requested: str | None = None
        self.compact_requested: str | None = None
        # /resend produces a model request, so it leaves the command path here.
        self.resend_requested = False
        self.mcp_enable_requested: str | None = None
        self.mcp_enabling: str | None = None
        self.model_requested = False
        self.pending_model: str | None = None
        self.registry = CommandRegistry()
        for command in (
            Command(
                "/login",
                "Sign in to Anthropic in a browser (/login pi reuses pi's login)",
                self.login,
                ("anthropic", "pi"),
            ),
            Command("/logout", "Remove pcode's stored Anthropic login", self.logout),
            Command(
                "/model",
                "Choose a model (Ctrl+L); keeps the conversation, applies next request",
                self.select_model,
            ),
            Command("/help", "Commands and keyboard shortcuts", self.help),
            Command("/tools", "Inspect tool calls and their results", self.tools, ("failed",)),
            Command("/errors", "Inspect failed tool calls", lambda _: self.tools("failed")),
            Command(
                "/edits",
                "Show/hide edit diffs and previews; redraw scrollback",
                self.show_edits,
                ("show", "hide"),
            ),
            Command("/demo", "Sample Markdown, code, diff, and tool output", self.demo),
            Command(
                "/redraw",
                "Rebuild retained scrollback (clears terminal history)",
                lambda _: self.transcript.regenerate(),
            ),
            Command(
                "/config",
                "Inspect or edit global defaults (next launch)",
                self.config,
                free_arguments=True,
                argument_provider=config_arguments,
            ),
            Command(
                "/show-tasks",
                "Show the Tasks/Tools widget: on / off (Ctrl+O)",
                self.show_tasks,
                ("on", "off"),
            ),
            Command(
                "/autohide-tasks",
                "Hide the Tasks/Tools widget when a turn ends: on / off (default on)",
                self.autohide_tasks,
                ("on", "off"),
            ),
            Command(
                "/show-thinking",
                "Show saved thinking in scrollback: on / off (Ctrl+T)",
                self.show_thinking,
                ("on", "off"),
            ),
            Command(
                "/show-commands",
                "Mirror commands and output to scrollback: on / off (Ctrl+G)",
                self.show_commands,
                ("on", "off"),
            ),
            Command("/theme", "Switch palette: dark / light / auto", self.theme, THEMES),
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
            Command(
                "/resend",
                "Ask the model again from the last saved checkpoint, without a new message",
                self.resend,
            ),
            Command("/context", "Model, workspace, and session usage", self.context),
            Command("/new", "Start a new saved conversation; keep transcript", self.new),
            Command("/tree", "Navigate and fork the conversation interactively", self.select_tree),
            Command("/session", "Choose a saved session to resume", self.select_session),
            Command("/quit", "Leave the terminal", self.quit, aliases=("/exit",)),
        ):
            self.registry.register(command)

    def _create_runtime(self):
        """Import and construct the backend off the terminal's event loop."""
        from pcode.agent import create_agent
        from pcode.live import AgentRuntime
        from pcode.sessions import SavedSession

        return AgentRuntime(
            create_agent(self.model, self.workspace),
            self._saved_session,
            session_factory=(
                lambda: SavedSession.create(self.model, self.workspace, self.session_dir)
            )
            if self.save_sessions
            else None,
        )

    async def _initialize_runtime(self) -> None:
        if self._needs_runtime:
            # A cancelled to_thread await does not stop its thread. Keep ownership
            # until it finishes so a late-created runtime cannot leak on exit.
            task = asyncio.create_task(asyncio.to_thread(self._create_runtime))
            try:
                runtime = await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    runtime = await task
                except Exception:
                    pass
                else:
                    runtime.close()
                raise
            self.runtime = runtime
            self._needs_runtime = False
            agent = getattr(runtime, "agent", None)
            if agent is not None:
                apply_effort(agent, self.model, load_preferences().get("effort"))
                apply_thinking(agent, self.model, self.activity.show_thinking)
        if self.resuming:
            await self.runtime.restore()

    def compact(self, argument: str, *, before_queue: bool = False) -> None:
        if not self.model or not hasattr(self.runtime, "compact"):
            raise ValueError("/compact requires a live model session.")
        if not before_queue and (self.activity.busy or self.activity.queued_prompts):
            raise ValueError("/compact is unavailable while working. Cancel or wait, then retry.")
        self.compact_requested = argument

    def resend(self, argument: str, *, before_queue: bool = False) -> None:
        """Ask again from the settled checkpoint instead of typing "continue"."""
        if argument:
            raise ValueError("/resend takes no arguments.")
        if not self.model or not hasattr(self.runtime, "resend_prompt"):
            raise ValueError("/resend requires a live model session.")
        if not before_queue and (self.activity.busy or self.activity.queued_prompts):
            raise ValueError("/resend is unavailable while working. Cancel or wait, then retry.")
        self.runtime.resend_prompt()
        self.resend_requested = True

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

    def set_show_tasks(self, shown: bool) -> None:
        self.activity.show_tasks = shown
        self.persist_defaults(show_tasks="on" if shown else "off")
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()

    def show_tasks(self, argument: str) -> None:
        if argument:
            if argument not in ("on", "off"):
                raise ValueError("Usage: /show-tasks on|off")
            self.set_show_tasks(argument == "on")
        state = "on" if self.activity.show_tasks else "off"
        self.transcript.note(f"Show tasks: {state}. Usage: /show-tasks on|off (Ctrl+O)")

    def autohide_tasks(self, argument: str) -> None:
        if argument:
            if argument not in ("on", "off"):
                raise ValueError("Usage: /autohide-tasks on|off")
            self.activity.autohide_tasks = argument == "on"
            if not self.activity.autohide_tasks:
                self.activity.tasks_autohidden = False
            self.persist_defaults(autohide_tasks=argument)
            if self.transcript.output is not None:
                self.transcript.output.app.invalidate()
        state = "on" if self.activity.autohide_tasks else "off"
        self.transcript.note(
            f"Auto-hide tasks after each turn: {state}. Usage: /autohide-tasks on|off"
        )

    def show_edits(self, argument: str) -> None:
        if argument and argument not in ("show", "hide"):
            raise ValueError("Usage: /edits [show|hide]")
        shown = argument == "show" if argument else not self.transcript.show_edits
        self.transcript.show_edits = shown
        self.persist_defaults(edits="show" if shown else "hide")
        self.transcript.regenerate()
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()

    def set_show_thinking(self, shown: bool) -> None:
        self.activity.show_thinking = shown
        agent = getattr(self.runtime, "agent", None)
        if agent is not None and self.model:
            apply_thinking(agent, self.model, shown)
        self.persist_defaults(show_thinking="on" if shown else "off")
        self.transcript.regenerate()
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()

    def show_thinking(self, argument: str) -> None:
        if argument:
            if argument not in ("on", "off"):
                raise ValueError("Usage: /show-thinking on|off")
            self.set_show_thinking(argument == "on")
        state = "on" if self.activity.show_thinking else "off"
        self.transcript.note(f"Show thinking: {state}. Usage: /show-thinking on|off (Ctrl+T)")
        if (self.model or "").startswith("anthropic:"):
            self.transcript.note(
                "Anthropic thinking request: "
                + ("enabled" if self.activity.show_thinking else "provider default")
                + " (next turn). Enabling thinking can increase latency and token usage."
            )
        if self.activity.show_thinking and (self.model or "").startswith("meridian:"):
            self.transcript.note(
                "Meridian must forward readable thinking for scrollback. "
                "Managed Meridian enables Thinking Passthrough in its private instance. "
                "For an external proxy, check passthrough → Thinking Passthrough in "
                "Meridian's /settings page; this toggle only changes pcode's display."
            )

    def cycle_send_mode(self) -> None:
        from pcode.preferences import SEND_MODES

        self.send_mode = SEND_MODES[(SEND_MODES.index(self.send_mode) + 1) % len(SEND_MODES)]
        self.persist_defaults(send_mode=self.send_mode)

    def toggle_command_scrollback(self) -> None:
        self.set_command_scrollback(not self.transcript.command_scrollback)
        self.show_commands("")

    def set_command_scrollback(self, shown: bool) -> None:
        # Reproject retained results as well as future completions.
        self.transcript.command_scrollback = shown
        self.persist_defaults(command_scrollback="on" if shown else "off")
        self.transcript.regenerate()
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()

    def show_commands(self, argument: str) -> None:
        if argument:
            if argument not in ("on", "off"):
                raise ValueError("Usage: /show-commands on|off")
            self.set_command_scrollback(argument == "on")
        state = "on" if self.transcript.command_scrollback else "off"
        self.transcript.note(
            f"Command output in scrollback: {state}. Usage: /show-commands on|off (Ctrl+G)"
        )

    def persist_defaults(self, **updates: str) -> None:
        try:
            save_preferences(**updates)
        except (OSError, ValueError):
            self.transcript.warning("Could not save defaults; this selection applies only here.")

    def forget_defaults(self, *keys: str) -> None:
        from pcode.preferences import update_preferences

        try:
            update_preferences({}, remove=keys)
        except (OSError, ValueError):
            self.transcript.warning("Could not update defaults; this change applies only here.")

    def select_model(self, argument: str) -> None:
        self.model_requested = True

    async def switch_model(self, model: str) -> None:
        """Adopt a model now, or record it for the next request while working."""
        if self.activity.busy or self.activity.queued_prompts:
            # Replacing the agent mid-run would change the model of a request
            # that is already in flight. Defer like /effort instead of refusing.
            if model == self.model:
                self.pending_model = None
                self.persist_defaults(model=model)
                self.transcript.note(f"Already using {model}.")
                return
            self.pending_model = model
            self.transcript.note(
                f"Model: {model} (next request). This turn finishes on {self.model or 'preview'}."
            )
            return
        await self.activate_model(model)

    async def apply_pending_model(self) -> None:
        """Adopt a model chosen mid-run, now that no request is in flight."""
        model, self.pending_model = self.pending_model, None
        if model is None:
            return
        try:
            await self.activate_model(model)
        except Exception as error:
            from pcode.live import error_message

            self.transcript.error(error_message(error), title="Model unchanged")

    async def activate_model(self, model: str) -> None:
        from pcode.agent import create_agent
        from pcode.live import AgentRuntime
        from pcode.sessions import SavedSession

        self.pending_model = None
        if model == self.model:
            self.persist_defaults(model=model)
            self.transcript.note(f"Already using {model}.")
            return
        # Construct first: a missing provider/login must leave the old session intact.
        agent = await asyncio.to_thread(create_agent, model, self.workspace)
        apply_effort(agent, model, load_preferences().get("effort"))
        apply_thinking(agent, model, self.activity.show_thinking)
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
        self.warn_without_credentials()

    async def choose_model(self, output: TerminalOutput, session) -> None:
        from pcode.model_ui import ModelPicker
        from pcode.models import active_providers, model_catalog

        self.model_requested = False
        providers = await asyncio.to_thread(active_providers, self.model)
        if not providers:
            self.transcript.note(
                "No active model providers. Use /login to sign in to Anthropic, "
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
        # Signing in stores a credential; it does not require the conversation to
        # already be on Anthropic. A non-Anthropic session keeps its own model.
        source = argument.strip() or "anthropic"
        if source not in {"anthropic", "pi"}:
            self.transcript.note("Usage: /login [anthropic | pi]")
            return
        self.login_requested = source

    def logout(self, argument: str) -> None:
        from pcode.anthropic_oauth import credentials_path, delete_tokens
        from pcode.auth import LoginError

        try:
            removed = delete_tokens(credentials_path())
        except LoginError as error:
            self.transcript.error(str(error))
            return
        if os.environ.get("PCODE_ANTHROPIC_AUTH", "").strip() == "oauth":
            del os.environ["PCODE_ANTHROPIC_AUTH"]
        # The stored sign-in is gone; a saved "oauth" choice would now resolve
        # to a credential that no longer exists.
        if load_preferences().get("anthropic_auth") == "oauth":
            self.forget_defaults("anthropic_auth")
        if not removed:
            self.transcript.note("No stored Anthropic login to remove.")
            return
        self.transcript.note(
            "Removed pcode's stored Anthropic login. This conversation keeps its current "
            "model until the token expires; use /login again or set ANTHROPIC_API_KEY."
        )

    async def perform_login(self) -> None:
        source, self.login_requested = self.login_requested, None
        if source == "pi":
            await self.login_pi()
        else:
            await self.login_anthropic()

    async def login_anthropic(self) -> None:
        from pcode.anthropic_oauth import AnthropicOAuthModel, credentials_path, login
        from pcode.auth import LoginError

        self.login_requested = None
        self.transcript.note(
            "Opening claude.ai to sign in with your Anthropic account. "
            "If no browser opens, visit this URL (Ctrl+C cancels):"
        )
        try:
            await login(notify=self.transcript.note)
            # Only an Anthropic conversation adopts the new credential; a Codex
            # or Meridian session keeps its own model and provider.
            if self.model and self.model.startswith("anthropic:"):
                self.runtime.agent.model = await asyncio.to_thread(AnthropicOAuthModel, self.model)
            os.environ["PCODE_ANTHROPIC_AUTH"] = "oauth"
            self.persist_defaults(anthropic_auth="oauth")
            self.transcript.note(
                f"Signed in to Anthropic. Credentials are stored in {credentials_path()} "
                "(owner-only) and refreshed automatically; /logout removes them."
            )
            self.transcript.note(
                "Future launches use this login automatically. "
                "Set PCODE_ANTHROPIC_AUTH=api-key to use ANTHROPIC_API_KEY instead."
            )
        except asyncio.CancelledError:
            self.transcript.note("Anthropic sign-in cancelled.")
            raise
        except LoginError as error:
            self.transcript.error(str(error))
        except Exception:
            self.transcript.error("Anthropic sign-in failed. No credential details were logged.")

    async def login_pi(self) -> None:
        self.login_requested = None
        from pcode.auth import LoginError
        from pcode.pi_auth import PiAnthropicModel, pi_auth_path, read_pi_credential

        try:
            if self.model:
                model = await asyncio.to_thread(PiAnthropicModel, self.model)
                self.runtime.agent.model = model
            else:
                await asyncio.to_thread(read_pi_credential, pi_auth_path())
            os.environ["PCODE_ANTHROPIC_AUTH"] = "pi"
            self.persist_defaults(anthropic_auth="pi")
            self.transcript.note(
                "Using pi's Anthropic credential (read-only). "
                "Refresh/login in pi when it expires; pcode never writes pi's auth file."
            )
            self.transcript.note(
                "Future launches reuse pi while its auth file exists. "
                "Set PCODE_ANTHROPIC_AUTH=api-key to opt back out."
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
            if isinstance(event, EditPreview):
                self.activity.edit_previews.pop(event.call_id, None)
                if event.path:
                    self.activity.edit_previews[event.call_id] = event
            elif isinstance(event, EditCompleted):
                self.transcript.edit(event)
            elif isinstance(event, CommandOutput):
                self.activity.command_outputs.pop(event.call_id, None)
                self.activity.command_outputs[event.call_id] = event
            elif isinstance(event, (ToolStarted, ToolSummary)):
                self.activity.tools.record(event)
                if self.transcript.output is not None:
                    self.transcript.output.app.invalidate()
                # Decide only after completion. The adapter's failed flag includes
                # non-zero shell exits, retries, and known tool validation failures.
                if isinstance(event, ToolSummary):
                    self.activity.command_outputs.pop(event.call_id, None)
                    self.transcript.tool_result(event)
            else:
                self.transcript.events((event,))

    def demo(self, argument: str) -> None:
        self.present_events(self.preview.demo())

    def theme(self, argument: str) -> None:
        self.transcript.theme = argument or (
            "light" if self.transcript.resolved_theme == "dark" else "dark"
        )
        self.persist_defaults(theme=self.transcript.theme)
        selected = self.transcript.theme
        if selected == "auto":
            selected += f" ({self.transcript.resolved_theme})"
        self.transcript.note(f"Theme: {selected}.")
        self.transcript.regenerate()

    def colors(self, argument: str) -> None:
        if argument:
            self.transcript.color_style = argument
        self.transcript.note(f"Colors: {self.transcript.color_style}.")
        self.transcript.regenerate()

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
            apply_thinking(agent, saved.info.model, self.activity.show_thinking)
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
        self.activity.prompt_kind = "user"
        self.activity.prompt_detail = ""
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

    def replay(self) -> None:
        from pcode.diagnostics import redact

        saved = self.runtime.session
        self.activity.plan = saved.latest_plan()
        self.transcript.note(f"Resumed {saved.info.id}; showing recent transcript.")
        for record in saved.recent_transcript():
            kind = record["kind"]
            if kind == "turn_started":
                self.transcript.user(redact(record["prompt"]))
            elif kind in ("Thinking", "thinking_partial"):
                self.transcript.thinking(redact(record["text"]).rstrip("\n") + "\n\n")
            elif kind == "EditCompleted":
                self.transcript.edit(
                    EditCompleted(
                        **{
                            key: value
                            for key, value in record.items()
                            if key in EditCompleted.__dataclass_fields__
                        }
                    )
                )
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
        if self.pending_model:
            # The running turn keeps its model; show what the next one will use.
            model += f" → {self.pending_model}"
        # Put send mode and activity ahead of model/path metadata so they are
        # never pushed off the footer by long provider names or narrow panes.
        segments = [("text", f"send: {self.send_mode}")]
        if self._startup_pending:
            segments.extend([("text", " · "), ("activity", "starting")])
        if self.activity.busy:
            segments.extend([("text", " · "), ("activity", "working")])
            if self.activity.queued:
                steering = self.activity.queued_modes.count("steering")
                queued = self.activity.queued - steering
                if steering:
                    segments.extend([("text", " · "), ("activity", f"{steering} steering pending")])
                if queued:
                    segments.extend([("text", " · "), ("activity", f"{queued} queued")])
        segments.extend(
            [
                ("text", " · "),
                ("model", plain(model, limit=None)),
                ("text", plain(f" · effort: {effort}", limit=None)),
            ]
        )
        context = ""
        if self.model and not self._startup_pending and self._startup_error is None:
            from pcode.context_usage import context_label

            resolved = getattr(getattr(self.runtime, "agent", None), "model", None)
            history = getattr(self.runtime, "context_history", None)
            if history is None:
                history = getattr(self.runtime, "history", ())
            context = context_label(resolved or self.model, history)
        segments.append(("text", context))
        details = "".join(value for _, value in segments)
        # Only spend spare width on the path; preserve the send mode first.
        path_width = max(0, width - cell_len(details) - 4)
        path = Text(location if path_width else "")
        if path_width:
            path.truncate(path_width, overflow="ellipsis")
        text = Text(f" {path.plain} · {details}" if path.plain else f" {details}")
        text.truncate(width, overflow="ellipsis")
        prefix = [("text", " ")]
        if path.plain:
            prefix.extend([("location", path.plain), ("text", " · ")])
        segments = prefix + segments
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
            return False
        self.transcript.user(text)
        self.present_events(self.preview.reply(text))
        return False

    async def run_live(self, output: TerminalOutput, text: str, *, resend: bool = False) -> bool:
        from pcode.live import error_message

        output.begin_turn(text)
        self.activity.start_prompt(text)
        self.activity.status = "Waiting for model…"

        def compaction_notice(text):
            self.activity.status = text
            self.transcript.note(text)

        self.runtime.compaction_notice = compaction_notice

        def retry_notice(text):
            # Separate abandoned partial text/thinking from the next attempt.
            output.finish_thinking()
            output.finish()
            self.activity.plan_preview = None
            self.activity.edit_previews.clear()
            self.activity.status = text
            self.transcript.note(text)
            output.app.invalidate()

        if hasattr(self.runtime, "retry_notice"):
            self.runtime.retry_notice = retry_notice
        failure = None
        cancelled = False
        try:
            async with aclosing(self.runtime.stream(None if resend else text)) as stream:
                async for event in stream:
                    if isinstance(event, ThinkingDelta):
                        output.finish()
                        output.thinking_delta(event.text)
                    elif isinstance(event, Thinking):
                        output.finish_thinking(event.text)
                    elif isinstance(event, TextDelta):
                        output.finish_thinking()
                        output.delta(event.text)
                        self.activity.status = "Responding…"
                    elif isinstance(event, EditCompleted):
                        output.finish_thinking()
                        output.finish()
                        self.present_events((event,))
                    elif isinstance(event, (CommandOutput, EditPreview)):
                        self.present_events((event,))
                    elif isinstance(event, RunStatus):
                        self.activity.status = event.text
                    elif isinstance(event, PlanUpdated):
                        self.activity.plan = event.items
                    elif isinstance(event, PlanPreview):
                        self.activity.plan_preview = event.items
                    elif isinstance(event, (ToolStarted, ToolSummary)):
                        output.finish_thinking()
                        # Hidden commands, including failures, do not interrupt prose.
                        if isinstance(event, ToolSummary) and (
                            (event.failed and event.name not in COMMAND_TOOLS)
                            or self.transcript.streams_command(event)
                        ):
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
            self.activity.edit_previews.clear()
            self.activity.command_outputs.clear()
            self.activity.plan_preview = None
            output.end_turn()
            self.activity.tools.interrupt_running()
            self.activity.status = ""
        self.activity.finish_prompt("cancelled" if cancelled else "failed" if failure else "done")
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

    def warn_without_credentials(self) -> None:
        """Say so at startup, not on the first prompt.

        An `anthropic:` model with no selected credential is built with
        `defer_model_check`, so its agent keeps the unresolved model string and
        the terminal opens looking healthy. Report it while /login is still the
        obvious next step.
        """
        if not (self.model or "").startswith("anthropic:"):
            return
        agent = getattr(self.runtime, "agent", None)
        if agent is None or not isinstance(getattr(agent, "model", None), str):
            return
        self.transcript.warning(
            "No Anthropic credential is selected, so prompts will fail. "
            "Run /login (anthropic or pi), or restart with ANTHROPIC_API_KEY set."
        )

    async def run_async(self) -> None:
        # This frontend owns the terminal; suppress the framework's unsolicited banner.
        os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
        self.transcript.welcome(self.model, str(self.workspace))
        ready = asyncio.Event()
        queue = asyncio.Queue()
        commands = asyncio.Queue()
        startup_commands = []
        command_idle = asyncio.Event()
        command_idle.set()
        live_task = None
        mcp_task = None
        compact_task = None
        compact_idle = asyncio.Event()
        compact_idle.set()
        pending_model_command = 0
        mcp_idle = asyncio.Event()
        mcp_idle.set()
        queue_generation = 0
        pending_mcp = 0
        interrupt_pending = False

        async def refresh_metadata():
            refresh = getattr(self.runtime, "refresh_context", None)
            if refresh is not None:
                try:
                    await refresh()
                except Exception:
                    # Optional metadata must not take down a usable editor.
                    pass
                finally:
                    session.app.invalidate()

        async def initialize():
            try:
                await self._initialize_runtime()
                self.show_startup_context()
                self.warn_without_credentials()
                saved = getattr(self.runtime, "session", None)
                if self.model and saved:
                    self.transcript.note(f"Saving session: {saved.info.id}")
                if self.resuming:
                    self.replay()
            except Exception as error:
                self._startup_error = error
                try:
                    from pcode.live import error_message

                    message = error_message(error)
                except ImportError:
                    message = "Provider dependency missing. Reinstall pcode and try again."
                self.transcript.error(message, title="Agent startup failed")
                clear_queue()
                self.activity.busy = False
            else:
                session.app.create_background_task(refresh_metadata())
            finally:
                self._startup_pending = False
                ready.set()
                for command in startup_commands:
                    commands.put_nowait(command)
                startup_commands.clear()
                session.app.invalidate()

        def clear_queue():
            nonlocal queue_generation, pending_mcp, pending_model_command
            queue_generation += 1
            startup_commands.clear()
            if commands.empty():
                command_idle.set()
            if pending_mcp:
                self.transcript.warning("Pending MCP enable command cancelled.")
                pending_mcp = 0
            if pending_model_command:
                self.transcript.warning("Pending model command cancelled.")
                pending_model_command = 0
            count = len(self.activity.queued_prompts)
            while not queue.empty():
                queue.get_nowait()
            self.activity.queued_prompts.clear()
            self.activity.queued_modes.clear()
            self.activity.queued = 0
            if count:
                self.transcript.note(f"Cleared {count} queued message(s).")

        def cancel():
            nonlocal interrupt_pending
            interrupt_pending = False
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

        def take_steering():
            pending = []
            messages = []
            while not queue.empty():
                generation, text, mode = queue.get_nowait()
                if generation == queue_generation and mode == "steering":
                    messages.append(text)
                    index = next(
                        i
                        for i, item in enumerate(
                            zip(self.activity.queued_prompts, self.activity.queued_modes)
                        )
                        if item == (text, mode)
                    )
                    self.activity.queued_prompts.pop(index)
                    self.activity.queued_modes.pop(index)
                    self.activity.start_prompt(text)
                    self.transcript.user(text)
                else:
                    pending.append((generation, text, mode))
            for item in pending:
                queue.put_nowait(item)
            self.activity.queued = len(self.activity.queued_prompts)
            if messages:
                session.app.invalidate()
            return messages

        def submit(text):
            nonlocal pending_mcp, pending_model_command, interrupt_pending
            text = text.strip()
            if not ready.is_set() and text in {"/quit", "/exit"}:
                # Do not strand exit behind a command waiting for initialization.
                self.running = False
                session.app.exit()
                return
            if text.startswith("/"):
                commands.put_nowait(
                    (
                        queue_generation,
                        text,
                        not self.activity.busy and not self.activity.queued_prompts,
                    )
                )
                command_idle.clear()
                if text.split()[0] in {"/compact", "/resend"}:
                    pending_model_command += 1
                    self.activity.busy = True
                if text.split()[:2] == ["/mcp", "enable"]:
                    pending_mcp += 1
                    # Enter + Ctrl+C in one input batch must cancel activation
                    # before its command worker has had a chance to start OAuth.
                    self.activity.busy = True
            elif text:
                if self.send_mode == "interrupt" and live_task and not live_task.done():
                    clear_queue()
                    interrupt_pending = True
                    if not live_task.cancelling():
                        live_task.cancel()
                queue.put_nowait((queue_generation, text, self.send_mode))
                self.activity.queued_prompts.append(text)
                self.activity.queued_modes.append(self.send_mode)
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
                        or bool(pending_model_command)
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
            # Label the work instead of echoing "/compact <focus>", which reads
            # like the command was typed as part of a prompt.
            self.activity.start_prompt(
                SYSTEM_COMMAND_LABELS["/compact"], kind="system", detail=focus
            )
            self.transcript.note("Compacting context with the current model. Ctrl+C cancels.")

            def finished(task):
                nonlocal compact_task
                success = False
                try:
                    result = task.result()
                    self.transcript.note(result.description())
                    self.activity.finish_prompt("done")
                    success = True
                except asyncio.CancelledError:
                    self.activity.finish_prompt("cancelled")
                    self.transcript.warning("Compaction cancelled; history unchanged.")
                except Exception as error:
                    from pcode.live import error_message

                    self.activity.finish_prompt("failed")
                    self.transcript.error(error_message(error), title="Compaction failed")
                finally:
                    if not success:
                        clear_queue()
                    compact_task = None
                    self.activity.busy = (
                        bool(self.activity.queued_prompts)
                        or bool(pending_mcp)
                        or bool(pending_model_command)
                    )
                    self.activity.status = ""
                    compact_idle.set()
                    session.app.invalidate()

            compact_task = asyncio.create_task(self.runtime.compact(focus))
            compact_task.add_done_callback(finished)

        async def consume_commands():
            nonlocal pending_mcp, pending_model_command
            while self.running:
                generation, text, submitted_idle = await commands.get()
                try:
                    if text.split()[0] not in {
                        "/quit",
                        "/exit",
                        "/help",
                        "/theme",
                        "/colors",
                        "/show-thinking",
                        "/show-commands",
                        "/config",
                    }:
                        if not ready.is_set():
                            # Keep consuming frontend-only commands while backend
                            # commands wait, preserving their order for readiness.
                            startup_commands.append((generation, text, submitted_idle))
                            continue
                        if generation != queue_generation:
                            continue
                        if self._startup_error is not None:
                            self.transcript.warning("Agent startup failed; restart pcode to retry.")
                            continue
                    if text.split()[0] in {"/compact", "/resend"}:
                        if generation != queue_generation:
                            continue
                        pending_model_command -= 1
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
                        and command.name in {"/compact", "/resend"}
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
                            "/resend",
                            "/new",
                            "/session",
                            "/tree",
                            "/login",
                            "/logout",
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
                            handler = self.resend if command.name == "/resend" else self.compact
                            handler(parts[1] if len(parts) > 1 else "", before_queue=True)
                        else:
                            self.handle(text)
                        if self.resend_requested:
                            self.resend_requested = False
                            previous = self.runtime.resend_prompt()
                            # This command was submitted idle, before any prompts
                            # now queued behind it. Preserve that submission order.
                            following = []
                            while not queue.empty():
                                following.append(queue.get_nowait())
                            queue.put_nowait((queue_generation, previous, "resend"))
                            for item in following:
                                queue.put_nowait(item)
                            self.activity.queued_prompts.insert(0, previous)
                            self.activity.queued_modes.insert(0, "resend")
                            self.activity.queued = len(self.activity.queued_prompts)
                            self.activity.start_prompt(previous)
                            self.activity.busy = True
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
                            await self.perform_login()
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
                    if pending_mcp or pending_model_command:
                        self.activity.busy = True
                    if commands.empty() and not startup_commands:
                        command_idle.set()
                await output.flush()
                if not self.running and session.app.is_running:
                    session.app.exit()

        async def consume():
            nonlocal live_task, interrupt_pending
            await ready.wait()
            while self.running:
                await command_idle.wait()
                await mcp_idle.wait()
                await compact_idle.wait()
                generation, text, _mode = await queue.get()
                if self._startup_error is not None:
                    clear_queue()
                    self.activity.busy = False
                    self.transcript.warning("Agent startup failed; restart pcode to retry.")
                    continue
                await command_idle.wait()
                await mcp_idle.wait()
                await compact_idle.wait()
                if not self.running:
                    return
                if generation != queue_generation:
                    continue  # Cancelled while waiting for a command/modal.
                self.activity.queued_prompts.pop(0)
                self.activity.queued_modes.pop(0)
                self.activity.queued = len(self.activity.queued_prompts)
                # A model chosen mid-run takes effect here, before the request
                # that follows it is sent.
                if self.pending_model is not None:
                    await self.apply_pending_model()
                success = True
                try:
                    resend = _mode == "resend"
                    if resend or self.handle(text):
                        self.activity.start_prompt(text)
                        self.runtime.take_steering = take_steering
                        live_task = asyncio.create_task(
                            self.run_live(output, text, resend=True)
                            if resend
                            else self.run_live(output, text)
                        )
                        try:
                            success = await live_task
                        except asyncio.CancelledError:
                            # Cancellation before run_live's first instruction.
                            if not session.app.is_running:
                                return
                            success = False
                            self.activity.finish_prompt("cancelled")
                            self.transcript.cancelled()
                except Exception as error:
                    from pcode.live import error_message

                    self.transcript.error(error_message(error), title="Agent failed")
                    success = False
                finally:
                    live_task = None
                if not session.app.is_running:
                    return
                if not success and not interrupt_pending:
                    clear_queue()
                interrupt_pending = False
                self.activity.busy = (
                    bool(self.activity.queued_prompts)
                    or bool(pending_mcp)
                    or bool(pending_model_command)
                )
                # Adopt it as soon as the turn ends so the footer and /context
                # agree with what the next request will use.
                if self.pending_model is not None:
                    await self.apply_pending_model()
                await output.flush()
                if not self.running:
                    session.app.exit()

        session = create_prompt(
            self.registry,
            activity=self.activity,
            transcript=self.transcript,
            on_submit=submit,
            on_cancel=cancel,
            on_tasks=self.set_show_tasks,
            on_thinking=self.set_show_thinking,
            on_commands=self.toggle_command_scrollback,
            on_effort=self.adjust_effort,
            on_send_mode=self.cycle_send_mode,
            on_model=lambda: submit("/model"),
            bottom_toolbar=self.toolbar,
            vi_mode=load_preferences().get("editing_mode", "emacs") == "vi",
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
            session.app.create_background_task(initialize())
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
    parser.add_argument("--theme", choices=THEMES, default=load_preferences().get("theme", "dark"))
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
    if args.sessions:
        from pcode.sessions import list_sessions

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
            from pcode.sessions import SavedSession, SessionError

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
            raise ValueError("Workspace must be an existing directory.")
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
        if app is not None and app.model and hasattr(app.runtime, "close"):
            app.runtime.close()
        if saved is not None:
            saved.close()


if __name__ == "__main__":
    main()
