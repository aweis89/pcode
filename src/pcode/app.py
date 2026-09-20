"""Compose the terminal with either the offline preview or a real Coder agent."""

import argparse
import asyncio
import os
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import ExitStack, aclosing
from dataclasses import replace
from pathlib import Path

from prompt_toolkit.application import get_app
from prompt_toolkit.input import create_input
from prompt_toolkit.styles import DynamicStyle
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown
from rich.rule import Rule
from rich.text import Text

from pcode.commands import Command, CommandRegistry
from pcode.completion import SHELLS as COMPLETION_SHELLS
from pcode.config import USAGE as CONFIG_USAGE
from pcode.config import config_argument_descriptions, config_arguments, configure
from pcode.preferences import (
    SETTINGS,
    SYNTAX_THEMES,
    apply_effort,
    apply_thinking,
    effort_for,
    effort_setting,
    load_preferences,
    save_model_effort,
    save_preferences,
)
from pcode.runtime import (
    CacheBust,
    CommandOutput,
    EditCompleted,
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
from pcode.shell_mode import execute, shell_command
from pcode.stream_display import present_events, present_stream_event
from pcode.theme import THEMES
from pcode.tool_display import plain
from pcode.ui import (
    COLOR_STYLES,
    SYSTEM_COMMAND_LABELS,
    Activity,
    TerminalOutput,
    Transcript,
    create_prompt,
    suspended_editor,
)
from pcode.worktree import WORKTREES_DIR


def location_label(workspace: Path, branch: str) -> str:
    """Compact `path@branch` for the footer.

    A session worktree repeats itself three times (`…/.worktrees/pcode-abc123`
    plus branch `pcode-abc123`), so collapse it to the repository it belongs to
    and let the branch name the checkout: `~/p/pcode@abc123`.
    """
    path, label = workspace, branch
    if branch and path.name == branch and path.parent.name == WORKTREES_DIR:
        path = path.parent.parent
        label = branch.removeprefix(SESSION_WORKTREE_PREFIX) or branch
    try:
        relative = path.relative_to(Path.home())
        directory = "~" if relative == Path(".") else f"~/{relative}"
    except ValueError:
        directory = str(path)
    if not label or label == path.name:
        return directory
    return f"{directory}@{label}"


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
        initial_prompt: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.send_mode = load_preferences().get("send_mode", "steering")
        self.model = model
        self.initial_prompt = initial_prompt
        # Consumed by the first saved session so it shares its ID with the
        # worktree created for it; later `/new` sessions get their own.
        self._session_id = session_id
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
        self._startup_context_shown: set[str] = set()
        agent = getattr(self.runtime, "agent", None)
        if agent is not None and model:
            apply_effort(agent, model, effort_for(model))
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
        self.diffs_requested = False
        self.links_requested = False
        # Unsaved conversations have no journal to re-read, so keep their changes.
        self.edits: list[EditCompleted] = []
        self.session_requested = False
        self.session_info_requested = False
        self.tree_requested = False
        self.login_requested: str | None = None
        self.compact_requested: str | None = None
        # /resend produces a model request, so it leaves the command path here.
        self.resend_requested = False
        # A skill command is a prompt in disguise; it leaves the command path too.
        self.skill_requested: str | None = None
        self.mcp_enable_requested: str | None = None
        self.mcp_enabling: str | None = None
        # A slow command (git work, for example) handed off so the terminal can
        # show a labelled system row while it runs off the event loop.
        self.job_requested: tuple[str, str, Callable[[], list[str]]] | None = None
        self.model_requested = False
        self.pending_model: str | None = None
        # User extensions load with the runtime; their commands register once it exists.
        self.extensions = None
        self.extension_command_names: list[str] = []
        self.reload_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self.registry = CommandRegistry()
        for command in (
            Command(
                "/help",
                "List commands and keyboard shortcuts",
                self.help,
                aliases=("/commands",),
                group="App",
            ),
            Command(
                "/config",
                "Inspect or edit saved defaults: get KEY / set KEY VALUE / unset KEY / reset",
                self.config,
                free_arguments=True,
                argument_provider=config_arguments,
                argument_descriptions=config_argument_descriptions(),
                group="App",
            ),
            Command("/quit", "Exit pcode", self.quit, aliases=("/exit",), group="App"),
            Command(
                "/status",
                "Show model, workspace, session, and context usage",
                self.status,
                group="Inspect",
            ),
            Command(
                "/tools",
                "Browse tool calls and results; 'failed' shows only failures",
                self.tools,
                ("failed",),
                group="Inspect",
            ),
            Command("/diffs", "Browse this conversation's file diffs", self.diffs, group="Inspect"),
            Command(
                "/links",
                "Pick a URL from this conversation and open it in the browser",
                self.links,
                group="Inspect",
            ),
            Command(
                "/tree",
                "Browse the conversation tree and fork from any point",
                self.select_tree,
                group="Inspect",
            ),
            Command(
                "/model",
                "Choose a model; keeps the conversation (Ctrl+L)",
                self.select_model,
                group="Model",
            ),
            Command(
                "/effort",
                "Set reasoning effort: low / medium / high / xhigh / default (Ctrl+N / Ctrl+P)",
                self.effort,
                ("low", "medium", "high", "xhigh", "default"),
                group="Model",
            ),
            Command(
                "/mcp",
                "Manage MCP servers: list / enable NAME / disable NAME",
                self.mcp,
                ("list", "enable", "disable"),
                free_arguments=True,
                argument_provider=self.mcp_arguments,
                group="Model",
            ),
            Command(
                "/login",
                "Sign in to Anthropic in a browser",
                self.login,
                ("anthropic",),
                group="Model",
            ),
            Command("/logout", "Remove the stored Anthropic login", self.logout, group="Model"),
            Command(
                "/extensions",
                "List loaded extensions and where they come from",
                self.list_extensions,
                group="Model",
            ),
            Command(
                "/reload",
                "Reload extensions; keeps the conversation",
                self.reload,
                group="Model",
            ),
            Command(
                "/new", "Start a new conversation; clears the screen", self.new, group="Session"
            ),
            Command(
                "/resume",
                "Browse and search saved sessions to resume",
                self.select_session,
                group="Session",
            ),
            Command(
                "/compact",
                "Summarize older context now; optional FOCUS steers the summary",
                self.compact,
                free_arguments=True,
                group="Session",
            ),
            Command(
                "/autocompact",
                "Compact automatically near the context limit: on / off",
                self.autocompact,
                ("on", "off"),
                group="Session",
            ),
            Command(
                "/resend",
                "Ask the model again from the last checkpoint, without a new message",
                self.resend,
                group="Session",
            ),
            Command(
                "/worktree",
                "This session's git worktree: status / merge / resolve / finish / remove"
                " / list / clean",
                self.worktree,
                tuple(WORKTREE_ACTIONS),
                group="Session",
                argument_descriptions=WORKTREE_ACTIONS,
            ),
            Command(
                "/show-tasks",
                "Tasks/Tools widget: on / off; bare toggles (Ctrl+O)",
                self.show_tasks,
                ("on", "off"),
                group="Display",
            ),
            Command(
                "/autohide-tasks",
                "Hide the Tasks/Tools widget when a turn ends: on / off; bare toggles",
                self.autohide_tasks,
                ("on", "off"),
                group="Display",
            ),
            Command(
                "/show-thinking",
                "Thinking in scrollback: on / off; bare toggles (Ctrl+T)",
                self.show_thinking,
                ("on", "off"),
                group="Display",
            ),
            Command(
                "/show-edits",
                "Edit diffs and previews in scrollback: on / off; bare toggles",
                self.show_edits,
                ("on", "off"),
                group="Display",
            ),
            Command(
                "/show-commands",
                "Shell command output in scrollback: on / off; bare toggles (Ctrl+G)",
                self.show_commands,
                ("on", "off"),
                group="Display",
            ),
            Command(
                "/theme",
                "Set the palette: dark / light / auto; bare toggles dark/light",
                self.theme,
                THEMES,
                group="Display",
            ),
            Command(
                "/colors",
                "Set Rich colors: palette / terminal",
                self.colors,
                COLOR_STYLES,
                group="Display",
            ),
            Command(
                "/syntax",
                "Set the code, menu and prompt style for the active palette",
                self.syntax,
                SYNTAX_THEMES,
                group="Display",
            ),
            Command(
                "/theme-preview",
                "Sample output plus every syntax style and how to select one",
                self.theme_preview,
                group="Display",
            ),
            Command(
                "/redraw",
                "Rebuild scrollback at the current width and display settings",
                lambda _: self.transcript.regenerate(),
                group="Display",
            ),
        ):
            self.registry.register(command)
        self.register_skills()

    def register_skills(self) -> None:
        """Expose discovered SKILL.md assets as commands, skipping any collision."""
        from pcode.skills import discover_skills, skill_commands

        self.skill_command_names: list[str] = []
        for command in skill_commands(discover_skills(self.workspace), self.run_skill):
            names = (command.name, *command.aliases)
            # Bare names can collide with a built-in command; built-ins win, and
            # the prefixed form still reaches the skill.
            taken = [name for name in names if self.registry.find(name)]
            if command.name in taken:
                continue
            if taken:
                command = replace(
                    command, aliases=tuple(name for name in command.aliases if name not in taken)
                )
            self.registry.register(command)
            self.skill_command_names.append(command.name)

    def run_skill(self, skill, argument: str) -> None:
        from pcode.skills import skill_prompt

        if not self.model:
            raise ValueError(f"/skill:{skill.name} requires a live model session.")
        self.skill_requested = skill_prompt(skill, argument)

    def register_extension_commands(self) -> None:
        """Expose extension commands, replacing the previous load's; built-ins win."""
        for name in self.extension_command_names:
            self.registry.unregister(name)
        self.extension_command_names = []
        if self.extensions is None:
            return
        for extension in self.extensions.extensions:
            for command in extension.commands:
                names = (command.name, *command.aliases)
                if taken := [name for name in names if self.registry.find(name)]:
                    self.transcript.warning(
                        f"Extension {extension.name}: {', '.join(taken)} already exists; skipped."
                    )
                    continue
                self.registry.register(command)
                self.extension_command_names.append(command.name)

    def list_extensions(self, argument: str) -> None:
        from pcode.ext import PROJECT_DIR, user_extension_dir

        if argument:
            raise ValueError("/extensions takes no arguments.")
        if not self.model:
            raise ValueError("/extensions requires a live model session.")
        lines = self.extensions.report(self.workspace) if self.extensions else []
        if not lines:
            lines = ["No extensions loaded."]
        lines.append(f"User extensions: {user_extension_dir()}")
        from pcode.project_trust import is_trusted

        lines.append(
            f"Project extensions ({PROJECT_DIR}): "
            + (
                "on (repository trusted)"
                if is_trusted(self.workspace)
                else "off; answer the launch prompt or /config set project_extensions on"
            )
        )
        lines.append("Ask pcode to write one, then /reload.")
        self.transcript.note("\n".join(lines))

    def reload(self, argument: str) -> None:
        if argument:
            raise ValueError("/reload takes no arguments.")
        if not self.model or not hasattr(self.runtime, "replace_agent"):
            raise ValueError("/reload requires a live model session.")
        if self.activity.busy or self.activity.queued_prompts:
            raise ValueError("/reload is unavailable while working. Cancel or wait, then retry.")
        self.reload_requested = True

    async def reload_extensions(self) -> None:
        """Re-import every extension and rebuild the agent around the same conversation."""
        from pcode.agent import create_agent

        self.reload_requested = False
        loaded = await asyncio.to_thread(self._load_extensions)
        # Construct first, so a failure leaves the previous agent in place.
        agent = await asyncio.to_thread(
            create_agent, self.model, self.workspace, loaded.capabilities, loaded.subagents
        )
        apply_effort(agent, self.model, effort_for(self.model))
        apply_thinking(agent, self.model, self.activity.show_thinking)
        self.extensions = loaded
        self.runtime.replace_agent(agent)
        await self.runtime.refresh_context()
        self.register_extension_commands()
        count = len(loaded.extensions) - len(loaded.failed)
        summary = f"Reloaded {count} extension{'s' if count != 1 else ''}"
        if loaded.failed:
            summary += f", {len(loaded.failed)} failed"
        # A changed tool list or instruction invalidates the cached prompt prefix.
        self.transcript.note(f"{summary}. The next request rebuilds the prompt cache.")
        for line in loaded.report(self.workspace):
            self.transcript.note("Extension " + line)

    def _extension_notice(self, text: str, level: str) -> None:
        """Route an extension's notice to the transcript from any thread."""
        show = {"warning": self.transcript.warning, "error": self.transcript.error}.get(
            level, self.transcript.note
        )
        loop = self._loop
        if (
            loop is not None
            and loop.is_running()
            and threading.current_thread() is not threading.main_thread()
        ):
            loop.call_soon_threadsafe(show, text)
        else:
            show(text)

    def _load_extensions(self):
        from pcode.ext import ExtensionUI, load_extensions

        return load_extensions(
            self.workspace,
            ExtensionUI(self._extension_notice, lambda: self.reload("")),
            session_dir=self.session_dir,
        )

    def _create_runtime(self):
        """Import and construct the backend off the terminal's event loop."""
        from pcode.agent import create_agent
        from pcode.live import AgentRuntime

        self.extensions = self._load_extensions()
        return AgentRuntime(
            create_agent(
                self.model,
                self.workspace,
                self.extensions.capabilities,
                self.extensions.subagents,
            ),
            self._saved_session,
            session_factory=self._create_session if self.save_sessions else None,
        )

    def _create_session(self, model: str | None = None):
        from pcode.sessions import SavedSession

        identity, self._session_id = self._session_id, None
        return SavedSession.create(
            model or self.model, self.workspace, self.session_dir, identity=identity
        )

    async def _initialize_runtime(self) -> None:
        self._loop = asyncio.get_running_loop()
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
                apply_effort(agent, self.model, effort_for(self.model))
                apply_thinking(agent, self.model, self.activity.show_thinking)
            self.register_extension_commands()
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
        self.transcript.flash(f"Automatic compaction: {state}. Usage: /autocompact on|off")

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

    @staticmethod
    def toggle_argument(command: str, argument: str, current: bool) -> bool:
        """Resolve `on`/`off`, or flip `current` when the argument is empty."""
        if not argument:
            return not current
        if argument not in ("on", "off"):
            raise ValueError(f"Usage: {command} [on|off]")
        return argument == "on"

    def show_tasks(self, argument: str) -> None:
        self.set_show_tasks(self.toggle_argument("/show-tasks", argument, self.activity.show_tasks))
        state = "on" if self.activity.show_tasks else "off"
        self.transcript.flash(f"Show tasks: {state}. Usage: /show-tasks [on|off] (Ctrl+O)")

    def autohide_tasks(self, argument: str) -> None:
        enabled = self.toggle_argument("/autohide-tasks", argument, self.activity.autohide_tasks)
        self.activity.autohide_tasks = enabled
        if not enabled:
            self.activity.tasks_autohidden = False
        self.persist_defaults(autohide_tasks="on" if enabled else "off")
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()
        state = "on" if enabled else "off"
        self.transcript.flash(
            f"Auto-hide tasks after each turn: {state}. Usage: /autohide-tasks [on|off]"
        )

    def show_edits(self, argument: str) -> None:
        shown = self.toggle_argument("/show-edits", argument, self.transcript.show_edits)
        self.transcript.show_edits = shown
        self.persist_defaults(show_edits="on" if shown else "off")
        self.transcript.regenerate()
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()
        self.transcript.flash(
            f"Show edits: {'on' if shown else 'off'}. Usage: /show-edits [on|off]"
        )

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
        self.set_show_thinking(
            self.toggle_argument("/show-thinking", argument, self.activity.show_thinking)
        )
        state = "on" if self.activity.show_thinking else "off"
        lines = [f"Show thinking: {state}. Usage: /show-thinking [on|off] (Ctrl+T)"]
        if (self.model or "").startswith("anthropic:"):
            lines.append(
                "Anthropic thinking request: "
                + ("enabled" if self.activity.show_thinking else "provider default")
                + " (next turn). Enabling thinking can increase latency and token usage."
            )
        if self.activity.show_thinking and (self.model or "").startswith("meridian:"):
            lines.append(
                "Meridian must forward readable thinking for scrollback. "
                "Managed Meridian enables Thinking Passthrough in its private instance. "
                "For an external proxy, check passthrough → Thinking Passthrough in "
                "Meridian's /settings page; this toggle only changes pcode's display."
            )
        self.transcript.flash("\n".join(lines))

    def cycle_send_mode(self) -> None:
        from pcode.preferences import SEND_MODES

        self.send_mode = SEND_MODES[(SEND_MODES.index(self.send_mode) + 1) % len(SEND_MODES)]
        self.persist_defaults(send_mode=self.send_mode)

    def set_show_commands(self, shown: bool) -> None:
        # Reproject retained results as well as future completions.
        self.transcript.command_scrollback = shown
        self.persist_defaults(show_commands="on" if shown else "off")
        self.transcript.regenerate()
        if self.transcript.output is not None:
            self.transcript.output.app.invalidate()

    def show_commands(self, argument: str) -> None:
        self.set_show_commands(
            self.toggle_argument("/show-commands", argument, self.transcript.command_scrollback)
        )
        state = "on" if self.transcript.command_scrollback else "off"
        self.transcript.flash(f"Show commands: {state}. Usage: /show-commands [on|off] (Ctrl+G)")

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

        self.pending_model = None
        if model == self.model:
            self.persist_defaults(model=model)
            self.transcript.note(f"Already using {model}.")
            return
        # Construct first: a missing provider/login must leave the old session intact.
        capabilities = self.extensions.capabilities if self.extensions else ()
        subagents = self.extensions.subagents if self.extensions else ()
        agent = await asyncio.to_thread(
            create_agent, model, self.workspace, capabilities, subagents
        )
        apply_effort(agent, model, effort_for(model))
        apply_thinking(agent, model, self.activity.show_thinking)
        save = self.save_sessions or getattr(self.runtime, "session_factory", None) is not None
        factory = (lambda: self._create_session(model)) if save else None
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
            async with suspended_editor(session.app):
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
        if source != "anthropic":
            self.transcript.note("Usage: /login [anthropic]")
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
        self.login_requested = None
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
            async with suspended_editor(session.app):
                # The suspended editor can still have an escape-flush timer.
                # Give the modal its own parser, or that timer can steal an
                # early Escape from the shared input object's parser buffer.
                stdin = getattr(session.app.input, "stdin", None)
                modal_input = create_input(stdin=stdin) if stdin is not None else session.app.input
                try:
                    inspector = ToolInspector(
                        archive,
                        failed=failed,
                        rich_theme=self.transcript.rich_theme,
                        code_theme=self.transcript.code_theme,
                        color_system=self.transcript.console.color_system,
                        input=modal_input,
                        output=session.app.output,
                        style=session.app.style,
                    )
                    await inspector.run()
                finally:
                    if modal_input is not session.app.input:
                        modal_input.close()

    def diffs(self, argument: str) -> None:
        if argument:
            raise ValueError("Usage: /diffs")
        self.diffs_requested = True

    def links(self, argument: str) -> None:
        if argument:
            raise ValueError("Usage: /links")
        self.links_requested = True

    async def choose_link(self, output: TerminalOutput, session) -> None:
        from pcode.links import conversation_links, open_link
        from pcode.links_ui import links_dialog

        self.links_requested = False
        tree = getattr(self.runtime, "tree", None)
        links = conversation_links(tree) if tree is not None and tree.nodes else []
        if not links:
            self.transcript.note("No links in this conversation yet.")
            return
        await output.flush()
        async with output.lock:
            async with suspended_editor(session.app):
                stdin = getattr(session.app.input, "stdin", None)
                modal_input = create_input(stdin=stdin) if stdin is not None else session.app.input
                try:
                    dialog = links_dialog(
                        links,
                        input=modal_input,
                        output=session.app.output,
                        style=session.app.style,
                    )
                    url = await dialog.run_async()
                finally:
                    if modal_input is not session.app.input:
                        modal_input.close()
        if url is None:
            return
        try:
            open_link(url)
        except (OSError, RuntimeError) as error:
            self.transcript.error(f"Could not open {url}: {error}")
            return
        self.transcript.note(f"Opened {url}")

    def recorded_edits(self) -> list[EditCompleted]:
        """Prefer the saved journal on the active branch; fall back to this process."""
        from pcode.edits import change_from_record

        saved = getattr(self.runtime, "session", None)
        if saved is None:
            return list(self.edits)
        return [
            change_from_record(record)
            for record in saved.active_records()
            if record.get("kind") == "EditCompleted"
        ]

    async def browse_diffs(self, output: TerminalOutput, session) -> None:
        from pcode.edit_ui import EditBrowser

        self.diffs_requested = False
        changes = await asyncio.to_thread(self.recorded_edits)
        await output.flush()
        # One terminal owner: drain permanent output, suspend the editor, and
        # hold the writer lock until the alternate screen has been restored.
        async with output.lock:
            async with suspended_editor(session.app):
                # The suspended editor can still have an escape-flush timer.
                # Give the modal its own parser, or that timer can steal an
                # early Escape from the shared input object's parser buffer.
                stdin = getattr(session.app.input, "stdin", None)
                modal_input = create_input(stdin=stdin) if stdin is not None else session.app.input
                try:
                    browser = EditBrowser(
                        changes,
                        code_theme=self.transcript.code_theme,
                        input=modal_input,
                        output=session.app.output,
                        style=session.app.style,
                    )
                    await browser.run()
                finally:
                    if modal_input is not session.app.input:
                        modal_input.close()

    def help(self, argument: str) -> None:
        self.transcript.help(self.registry)

    def present_events(self, events) -> None:
        present_events(events, activity=self.activity, transcript=self.transcript, edits=self.edits)

    def theme_preview(self, argument: str) -> None:
        self.present_events(self.preview.demo())
        self.transcript.syntax_gallery()

    def theme(self, argument: str) -> None:
        self.transcript.theme = argument or (
            "light" if self.transcript.resolved_theme == "dark" else "dark"
        )
        self.persist_defaults(theme=self.transcript.theme)
        selected = self.transcript.theme
        if selected == "auto":
            selected += f" ({self.transcript.resolved_theme})"
        self.transcript.flash(f"Theme: {selected}.")
        self.transcript.regenerate()

    def colors(self, argument: str) -> None:
        if argument:
            self.transcript.color_style = argument
        self.transcript.flash(f"Colors: {self.transcript.color_style}.")
        self.transcript.regenerate()

    def syntax(self, argument: str) -> None:
        """Choose the Pygments style for code, the completion menu and the prompt.

        Each palette keeps its own style, so switching to the other palette and
        back restores the style picked for it rather than the last one set.
        """
        palette = self.transcript.resolved_theme
        if argument:
            SETTINGS[f"syntax_{palette}"].validate(f"syntax_{palette}", argument)
            self.transcript.syntax_themes[palette] = argument
            self.persist_defaults(**{f"syntax_{palette}": argument})
        selected = self.transcript.syntax_themes[palette]
        if self.transcript.color_style == "terminal":
            self.transcript.flash(
                f"Syntax ({palette}): {selected}, unused while /colors is terminal."
            )
        else:
            self.transcript.flash(f"Syntax ({palette}): {selected}.")
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
            self.transcript.flash(
                f"Effort: {self.current_effort()}. Usage: /effort low|medium|high|xhigh|default"
            )
            return
        if value not in ("low", "medium", "high", "xhigh", "default"):
            self.transcript.flash("Usage: /effort low|medium|high|xhigh|default")
            return
        agent = getattr(self.runtime, "agent", None)
        if agent is None or effort_setting(self.model) is None:
            self.transcript.flash(
                "Effort control requires an OpenAI/Codex, Anthropic, or Meridian model."
            )
            return
        # Replace rather than mutate: an active run keeps its captured settings.
        apply_effort(agent, self.model, value)
        self.persist_defaults(model=self.model)
        # Per model: raising effort on one model must not raise it on the next.
        try:
            save_model_effort(self.model, value)
        except (OSError, ValueError):
            self.transcript.warning("Could not save defaults; this selection applies only here.")
        self.transcript.flash(f"Effort: {self.current_effort()} (next turn).")

    def adjust_effort(self, direction: int) -> None:
        levels = ("low", "medium", "high", "xhigh")
        current = self.current_effort()
        # The provider default is unspecified; use medium as the starting point.
        index = levels.index(current) if current in levels else 1
        self.effort(levels[max(0, min(len(levels) - 1, index + direction))])

    def session_overview(self) -> list[tuple[str, str]]:
        """Label/value rows describing the live conversation.

        One source for the `/status` notes and popup, so the
        two can never drift into describing the same session differently.
        """
        if not self.model:
            return [
                ("Model", "none · tools: none · network: none"),
                ("Preview turns", str(self.runtime.turns)),
                ("Mode", "Canned replies only. Start with -m PROVIDER:MODEL for a real agent."),
            ]
        totals = self.runtime.totals
        rows = [
            ("Model", self.model),
            ("Effort", self.current_effort()),
            ("Workspace", str(self.workspace)),
            ("Turns", str(self.runtime.turns)),
            ("Tokens in/out", f"{self.runtime.input_tokens}/{self.runtime.output_tokens}"),
            # A cache read costs a fraction of an uncached token and a write costs
            # more than one, so the split is the part worth watching.
            (
                "Input cached read/write",
                f"{totals.cache_read}/{totals.cache_write} (uncached {totals.uncached_input})",
            ),
            ("Tools", "Coder tools enabled; no sandbox."),
            *self.overhead_overview(),
            (
                "Automatic compaction",
                ("on" if getattr(self.runtime, "auto_compact", False) else "off")
                + " · /compact [focus] · /autocompact on|off",
            ),
        ]
        saved = self.runtime.session
        if saved:
            rows += [
                ("Session", saved.info.id),
                ("Saved in", str(saved.directory)),
                ("Started", saved.info.created[:16]),
                ("Updated", saved.info.updated[:16]),
            ]
        elif self.runtime.session_factory is not None:
            rows.append(("Session", "Will be saved after your first prompt."))
        else:
            rows.append(("Session", "Saving disabled; in memory only."))
        tree = getattr(self.runtime, "tree", None)
        if tree and tree.nodes:
            rows.append(("Branches", f"{len(tree.nodes)} turns in /tree"))
        mcp = getattr(self.runtime, "mcp", None)
        if enabled := sorted(getattr(mcp, "enabled", ()) or ()):
            rows.append(("MCP", ", ".join(enabled)))
        return rows

    def overhead_overview(self) -> list[tuple[str, str]]:
        """Attribute the fixed part of the prompt: instructions, assets, tool schemas.

        Read from the last request rather than re-derived, so the rows describe
        what the provider was actually sent. Nothing is available before the
        first request, where the alternative would be a parallel guess at a
        system prompt only the agent flow can resolve.
        """
        from pcode.context_breakdown import overhead_rows
        from pcode.context_usage import context_window
        from pcode.model_metadata import ContextWindowError

        parameters = getattr(self.runtime, "request_parameters", None)
        if parameters is None:
            return [("Prompt overhead", "Measured on the first model request.")]
        try:
            resolved = getattr(getattr(self.runtime, "agent", None), "model", None)
            window = context_window(resolved or self.model)
        except ContextWindowError:
            window = None
        return overhead_rows(parameters, window=window)

    def status(self, argument: str) -> None:
        """Popup in the interactive editor; plain notes wherever there is no editor."""
        if argument:
            raise ValueError("Usage: /status")
        if self.transcript.output is not None:
            self.session_info_requested = True
            return
        for label, value in self.session_overview():
            self.transcript.note(f"{label}: {value}")

    def defer(self, label: str, detail: str, job: Callable[[], list[str]]) -> None:
        """Run a slow command's work under a system badge, or inline without a terminal.

        Handlers run on the terminal's event loop, so a job that takes seconds
        would freeze the screen with nothing to show for it. With a live
        terminal the work is picked up by the command loop, which paints a
        `◈ label ▸ detail` row (distinct from a model turn) and runs the job
        in a thread. The job returns lines for the transcript; a ValueError
        becomes the usual command error.
        """
        if self.transcript.output is None:
            for line in job():
                self.transcript.note(line)
            return
        self.job_requested = (label, detail, job)

    async def perform_job(self) -> None:
        assert self.job_requested is not None
        label, detail, job = self.job_requested
        self.job_requested = None
        output = self.transcript.output
        self.activity.busy = True
        self.activity.start_prompt(label, kind="system", detail=detail)
        if output is not None:
            output.app.invalidate()
        state = "failed"
        try:
            lines = await asyncio.to_thread(job)
            state = "done"
        except ValueError as error:
            self.transcript.error(str(error))
        else:
            for line in lines:
                self.transcript.note(line)
        finally:
            self.activity.finish_prompt(state)
            self.activity.busy = bool(self.activity.queued_prompts)
            if output is not None:
                output.app.invalidate()

    def worktree(self, argument: str) -> None:
        from pcode import worktree

        action = argument or "status"
        if action == "list":
            self.transcript.note(worktree.listing(self.workspace) or "Not a git repository.")
            return
        if action == "clean":
            # Works from the mainline too, where the leftovers are most visible.
            self.defer("Cleaning worktrees", "", lambda: worktree.clean(self.workspace))
            return
        linked = worktree.describe(self.workspace)
        if linked is None:
            self.transcript.note(
                f"{self.workspace} is not a linked worktree. Start one with "
                "`pcode --worktree` or `/config set worktree on`."
            )
            return
        if action == "status":
            dirty = worktree.is_dirty(linked.path)
            count = worktree.unmerged_commits(linked)
            self.transcript.note(f"Worktree: {linked.path} (branch {linked.branch})")
            mainline = worktree.mainline_branch(linked.main)
            self.transcript.note(f"Mainline: {linked.main} ({mainline})")
            self.transcript.note(
                f"{count} unmerged commit{'s' if count != 1 else ''}"
                + (", uncommitted changes" if dirty else "")
            )
            return
        if self.activity.busy:
            raise ValueError("Wait for the current turn to finish before changing the worktree.")
        if action == "resolve":
            if not self.model:
                raise ValueError("/worktree resolve needs a live model session.")
            files = worktree.conflicted_files(linked.path)
            if not files:
                raise ValueError("No merge conflicts to resolve; run /worktree merge first.")
            # A prompt in command clothing, dispatched like a skill.
            self.skill_requested = worktree.resolve_prompt(linked, files)
        elif action == "merge":
            self.defer("Merging worktree", linked.branch, lambda: [worktree.merge(linked)])
        elif action == "remove":
            if worktree.unmerged_commits(linked):
                raise ValueError("Branch has unmerged commits; /worktree merge first.")

            def remove() -> list[str]:
                result = worktree.remove(linked)
                self._leave_worktree(linked)
                return [result, "This session's workspace no longer exists; /quit."]

            self.defer("Removing worktree", linked.branch, remove)
        elif action == "finish":
            # Refusals raise before anything is deleted, so the session stays put.
            def finish() -> list[str]:
                result = worktree.finish(linked)
                self._leave_worktree(linked)
                self.running = False
                return [result]

            self.defer("Finishing worktree", linked.branch, finish)

    def _leave_worktree(self, linked) -> None:
        """Point the saved session at the mainline so `pcode -c` still finds a directory."""
        session = getattr(self.runtime, "session", None)
        if session is None:
            return
        session.info.workspace = str(linked.main)
        try:
            session.save_info()
        except OSError:
            pass

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
        self.edits.clear()
        self.transcript.clear()
        self.transcript.print(Rule("New conversation", style="pcode.muted"))
        self.transcript.note(
            "Context reset; MCP servers are off. Screen cleared; input history is unchanged."
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
            capabilities = self.extensions.capabilities if self.extensions else ()
            subagents = self.extensions.subagents if self.extensions else ()
            agent = create_agent(saved.info.model, self.workspace, capabilities, subagents)
            apply_effort(agent, saved.info.model, effort_for(saved.info.model))
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
            async with suspended_editor(session.app):
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
        from pcode.session_ui import SessionBrowser
        from pcode.sessions import list_sessions, session_root

        self.session_requested = False
        records = list_sessions(self.session_dir)
        if not any(Path(info.workspace).resolve() == self.workspace for info in records):
            self.transcript.note("No saved sessions for this workspace.")
            return
        current = getattr(self.runtime, "session", None)
        await output.flush()
        async with output.lock:
            async with suspended_editor(session.app):
                stdin = getattr(session.app.input, "stdin", None)
                modal_input = create_input(stdin=stdin) if stdin is not None else session.app.input
                try:
                    browser = SessionBrowser(
                        records,
                        root=self.session_dir or session_root(),
                        workspace=self.workspace,
                        active_id=current.info.id if current else None,
                        rich_theme=self.transcript.rich_theme,
                        code_theme=self.transcript.code_theme,
                        color_system=self.transcript.console.color_system,
                        input=modal_input,
                        output=session.app.output,
                        style=session.app.style,
                    )
                    identity = await browser.run()
                finally:
                    if modal_input is not session.app.input:
                        modal_input.close()
        if identity is not None:
            await self.resume_session(identity)

    async def show_session_info(self, output: TerminalOutput, session) -> None:
        from pcode.session_ui import session_info_dialog

        self.session_info_requested = False
        rows = self.session_overview()
        await output.flush()
        # One terminal owner: drain permanent output, suspend the editor, and
        # hold the writer lock until the alternate screen has been restored.
        async with output.lock:
            async with suspended_editor(session.app):
                # The suspended editor can still have an escape-flush timer.
                # Give the modal its own parser, or that timer can steal an
                # early Escape from the shared input object's parser buffer.
                stdin = getattr(session.app.input, "stdin", None)
                modal_input = create_input(stdin=stdin) if stdin is not None else session.app.input
                try:
                    dialog = session_info_dialog(
                        rows,
                        input=modal_input,
                        output=session.app.output,
                        style=session.app.style,
                    )
                    await dialog.run_async()
                finally:
                    if modal_input is not session.app.input:
                        modal_input.close()

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
            elif kind == "CacheBust":
                self.transcript.events((CacheBust(record["text"]),))
            elif kind == "EditCompleted":
                from pcode.edits import change_from_record

                self.transcript.edit(change_from_record(record))
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
                self.activity.tools.clear()
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
        self.activity.tools.clear()

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
        location = plain(location_label(self.workspace, self.branch), limit=None)
        model = self.model if self.model else "preview"
        if self.pending_model:
            # The running turn keeps its model; show what the next one will use.
            model += f" → {self.pending_model}"
        if self.model:
            model += f" ({self.current_effort()})"
        # Put send mode and activity ahead of model/path metadata so they are
        # never pushed off the footer by long provider names or narrow panes.
        segments = [("text", f"Enter: {self.send_mode}")]
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
        segments.extend([("text", " · "), ("model", plain(model, limit=None))])
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
                    present_stream_event(
                        event,
                        output=output,
                        transcript=self.transcript,
                        activity=self.activity,
                        present=self.present_events,
                    )
        except asyncio.CancelledError:
            cancelled = True
        except Exception as error:
            failure = error
        finally:
            self.activity.edit_previews.clear()
            self.activity.command_outputs.clear()
            self.activity.plan_preview = None
            output.end_turn()
            self.activity.tools.clear()
            self.activity.status = ""
        self.activity.finish_prompt("cancelled" if cancelled else "failed" if failure else "done")
        output.app.invalidate()
        if cancelled:
            self.transcript.cancelled()
        elif failure:
            self.transcript.error(error_message(failure), title="Agent failed")
        if (cancelled or failure) and self.runtime.session:
            directory = self.runtime.session.directory
            # Name the traceback file rather than the directory it sits in: the
            # frames are the point of looking, and a cancelled turn writes none.
            errors = directory / "errors.log"
            target = errors if failure and errors.exists() else directory
            self.transcript.note(f"Session and diagnostics: {target}")
            if self.runtime.recovery_blocked:
                self.transcript.warning(self.runtime.recovery_blocked)
        return not (cancelled or failure)

    async def run_shell(self, output: TerminalOutput, text: str) -> bool:
        """Run a `!command` the user typed and hand its result to the runtime.

        Output streams into the live command panel while it runs and is
        mirrored to scrollback when it ends. The model sees it on the next
        prompt, as a `shell` tool call, so ask a follow-up to discuss it.
        """
        # Lazy: pcode.shell pulls in the agent stack, which startup avoids.
        from pcode.shell import preview_text

        command = shell_command(text)
        assert command is not None
        call_id = f"shell_mode_{id(self):x}"
        cwd, env = (
            self.runtime.shell_environment()
            if hasattr(self.runtime, "shell_environment")
            else (self.workspace, None)
        )
        self.transcript.user(text)
        self.activity.start_prompt(text)
        self.activity.user_command = True
        self.activity.status = "Running command…"
        buffered = ""

        def show(chunk: str) -> None:
            nonlocal buffered
            buffered += chunk
            self.present_events((CommandOutput(call_id, command, preview_text(buffered)),))
            output.app.invalidate()

        run = None
        try:
            run = await execute(command, cwd=cwd, env=env, on_output=show)
        except asyncio.CancelledError:
            pass
        except OSError as error:
            self.transcript.error(str(error), title="Command failed to start")
        finally:
            self.activity.command_outputs.pop(call_id, None)
            self.activity.user_command = False
            self.activity.status = ""
        if run is None:
            self.activity.finish_prompt("cancelled")
            self.transcript.warning("Command cancelled; the model was not told about it.")
            output.app.invalidate()
            return False
        self.transcript.shell_result(
            command, run.output, failed=run.failed, elapsed_seconds=run.elapsed_seconds
        )
        if hasattr(self.runtime, "record_shell"):
            visible = await self.runtime.record_shell(run)
            reduced = not isinstance(visible, str) or visible != run.tool_result()
            self.transcript.note(
                "The model sees this command and its "
                + ("reduced output" if reduced else "output")
                + " with your next message."
            )
        self.activity.finish_prompt("done")
        output.app.invalidate()
        return True

    def show_startup_context(self) -> None:
        """Report repository instructions and skills, each line only once.

        A model switch re-runs this because starting without a model leaves
        nothing to report until a runtime exists. Repeating lines the
        transcript already carries is just noise, so only new ones print.
        """
        lines = []
        summary = getattr(self.runtime, "startup_context", None)
        if summary is not None:
            lines.extend(summary())
        if self.skill_command_names:
            lines.append("Skill commands: " + ", ".join(self.skill_command_names))
        warnings = []
        if self.extensions is not None:
            for extension, line in zip(
                self.extensions.extensions, self.extensions.report(self.workspace), strict=True
            ):
                # Shipped defaults are not news at every launch; /extensions lists them.
                if extension.loaded and extension.scope == "bundled":
                    continue
                (lines if extension.loaded else warnings).append("Extension " + line)
        for line in lines + warnings:
            if line in self._startup_context_shown:
                continue
            self._startup_context_shown.add(line)
            if line in warnings:
                self.transcript.warning(line)
            else:
                self.transcript.retained_note(line)

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
            "Run /login, or restart with ANTHROPIC_API_KEY set."
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
                    self.transcript.retained_note(f"Saving session: {saved.info.id}")
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
            elif shell_command(text) is not None:
                # Runs in turn, never as steering: its result rides the next
                # request rather than being spliced into a running one.
                queue.put_nowait((queue_generation, text, "shell"))
                self.activity.queued_prompts.append(text)
                self.activity.queued_modes.append("shell")
                self.activity.queued = len(self.activity.queued_prompts)
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
                        "/commands",
                        "/theme",
                        "/theme-preview",
                        "/colors",
                        "/syntax",
                        "/show-tasks",
                        "/autohide-tasks",
                        "/show-thinking",
                        "/show-edits",
                        "/show-commands",
                        "/redraw",
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
                            "/resume",
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
                        if self.job_requested is not None:
                            await self.perform_job()
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
                        if self.skill_requested is not None:
                            prompt = self.skill_requested
                            self.skill_requested = None
                            # Queue it like a typed message so send mode, steering,
                            # and cancellation keep their usual meaning.
                            queue.put_nowait((queue_generation, prompt, self.send_mode))
                            self.activity.queued_prompts.append(prompt)
                            self.activity.queued_modes.append(self.send_mode)
                            self.activity.queued = len(self.activity.queued_prompts)
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
                        if self.reload_requested:
                            await self.reload_extensions()
                        if self.login_requested:
                            await self.perform_login()
                        if self.tree_requested:
                            await self.choose_tree(output, session)
                        if self.session_requested:
                            await self.choose_session(output, session)
                        if self.session_info_requested:
                            await self.show_session_info(output, session)
                        if self.inspector_requested is not None:
                            await self.inspect_tools(output, session)
                        if self.diffs_requested:
                            await self.browse_diffs(output, session)
                        if self.links_requested:
                            await self.choose_link(output, session)
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
                    if _mode == "shell":
                        live_task = asyncio.create_task(self.run_shell(output, text))
                        try:
                            success = await live_task
                        except asyncio.CancelledError:
                            if not session.app.is_running:
                                return
                            success = False
                    elif resend or self.handle(text):
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
                # Adopt it as soon as the turn ends so the footer and /status
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
            workspace=self.workspace,
            on_submit=submit,
            on_cancel=cancel,
            on_tasks=self.set_show_tasks,
            on_thinking=self.set_show_thinking,
            on_commands=lambda: self.show_commands(""),
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
        session.app.style = DynamicStyle(lambda: self.transcript.prompt_style())

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
            if self.initial_prompt:
                # Queued like a typed message: it waits for the backend the same
                # way, and Ctrl+C clears it the same way.
                submit(self.initial_prompt)

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
            if self.extensions is not None:
                await self.extensions.close()
            await output.flush()
            self.transcript.output = None
        self.print_resume_hint()

    def print_resume_hint(self) -> None:
        saved = getattr(self.runtime, "session", None)
        if saved is None:
            self.transcript.console.print("Session not saved; no continue command available.")
        else:
            from pcode.sessions import session_root

            command = ["pcode", "--continue", saved.info.id]
            if saved.directory.parent.resolve() != session_root().resolve():
                command.extend(["--session-dir", str(saved.directory.parent.resolve())])
            self.transcript.console.print(f"Continue with: {shlex.join(command)}", markup=False)

    def run(self) -> None:
        asyncio.run(self.run_async())

    async def run_print_async(self, prompt: str, *, stdout=None) -> bool:
        """Answer one prompt without an editor: the reply to `stdout`, the rest to the transcript.

        The transcript console is expected to be stderr, so a pipe reading
        stdout sees the reply alone. A terminal gets rendered Markdown; a pipe
        gets its source, which is what a reader downstream can work with.
        Returns whether the turn succeeded.
        """
        from pcode.live import error_message

        os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
        stdout = sys.stdout if stdout is None else stdout
        reply = Console(file=stdout, theme=self.transcript.rich_theme)
        # Rendering replaces token-by-token output with settled blocks, so it
        # must not be chosen for a destination that cannot display it.
        console = reply if reply.is_terminal else None

        def write_reply(markdown: str, *, streamed: bool) -> None:
            """Settle one block of reply text; `streamed` means its source is already out."""
            if console is not None:
                console.print(Markdown(markdown, code_theme=self.transcript.code_theme))
                console.print()
                return
            if not streamed:
                stdout.write(markdown)
            if not markdown.endswith("\n"):
                stdout.write("\n")
            stdout.write("\n")
            stdout.flush()

        if not self.model:
            for event in self.preview.reply(prompt):
                if isinstance(event, Message):
                    write_reply(event.markdown, streamed=False)
            return True
        try:
            await self._initialize_runtime()
        except Exception as error:
            self.transcript.error(error_message(error), title="Agent startup failed")
            return False
        self.runtime.compaction_notice = self.transcript.note
        if hasattr(self.runtime, "retry_notice"):
            self.runtime.retry_notice = self.transcript.note
        # Text streamed since the last settled message, so a turn that ends
        # mid-block still prints what arrived.
        block = ""
        try:
            async with aclosing(self.runtime.stream(prompt)) as stream:
                async for event in stream:
                    if isinstance(event, TextDelta):
                        block += event.text
                        if console is None:
                            stdout.write(event.text)
                            stdout.flush()
                    elif isinstance(event, Message):
                        # Deltas usually carried this text already; a message
                        # without them (a structured result) is written whole.
                        write_reply(
                            event.markdown or block,
                            streamed=console is None and bool(block),
                        )
                        block = ""
                    elif isinstance(event, Thinking):
                        self.transcript.events((event,))
                    elif isinstance(event, (ThinkingDelta, RunStatus, PlanPreview, PlanUpdated)):
                        continue
                    else:
                        self.present_events((event,))
        except Exception as error:
            if block:
                write_reply(block, streamed=console is None)
            self.transcript.error(error_message(error), title="Agent failed")
            saved = getattr(self.runtime, "session", None)
            if saved is not None:
                self.transcript.note(f"Session and diagnostics: {saved.directory}")
            return False
        if block:
            write_reply(block, streamed=console is None)
        self.print_resume_hint()
        return True

    def run_print(self, prompt: str) -> bool:
        return asyncio.run(self.run_print_async(prompt))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Streaming terminal with a Coder agent",
        epilog=f"Global defaults, without starting a session: pcode {CONFIG_USAGE}",
    )
    # The workspace's `.pcode/preferences.json` overlays user defaults, so it has
    # to be known before the first load_preferences() (the --theme default).
    _select_project_root(sys.argv[1:])
    parser.add_argument(
        "--theme",
        choices=THEMES,
        default=load_preferences().get("theme", "dark"),
        help="Color theme (default: saved preference)",
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
    parser.add_argument(
        "--worktree",
        nargs="?",
        const=True,
        metavar="NAME",
        help="Work in a fresh .worktrees/NAME git worktree (default NAME: the session ID)",
    )
    parser.add_argument(
        "--no-worktree",
        action="store_true",
        help="Stay in the current checkout even when the worktree default is on",
    )
    # One positional list serves both the message and the `config` subcommand,
    # so an unquoted `pcode fix the bug` works and `config` needs no subparser.
    parser.add_argument(
        "prompt",
        nargs="*",
        metavar="PROMPT",
        help="Send this message first; with --print, read stdin when omitted",
    )
    parser.add_argument(
        "-p",
        "--print",
        action="store_true",
        help="Answer PROMPT without the editor: reply on stdout, tool activity on stderr",
    )
    parser.add_argument(
        "--theme-preview",
        # The flag was named --demo before it grew the style gallery; keep the
        # old spelling working for scripts and the Homebrew smoke test.
        "--demo",
        dest="theme_preview",
        action="store_true",
        help="Print an offline sample and the syntax-style gallery, then exit",
    )
    parser.add_argument("--sessions", action="store_true", help="List saved sessions and exit")
    parser.add_argument(
        "--completions",
        choices=COMPLETION_SHELLS,
        metavar="SHELL",
        help=f"Print a shell completion script ({', '.join(COMPLETION_SHELLS)}) and exit",
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="resume",
        nargs="?",
        const="latest",
        metavar="SESSION",
        help="Continue a session ID/prefix; omit SESSION for this directory's latest",
    )
    parser.add_argument(
        "--session-dir", type=Path, help="Override the private session storage directory"
    )
    parser.add_argument(
        "--no-save", action="store_true", help="Keep this live session in memory only"
    )
    parser.add_argument(
        "--profile",
        type=Path,
        metavar="DIR",
        help="Sample process-tree CPU/RSS to a new private DIR",
    )
    parser.add_argument(
        "--profile-cpu",
        action="store_true",
        help="Also trace function CPU time across threads (much slower; requires --profile)",
    )
    parser.add_argument(
        "--profile-memory",
        action="store_true",
        help="Also trace Python allocations (slower; requires --profile)",
    )
    args = parser.parse_args()
    if args.completions:
        from pcode.completion import install_hint, render

        print(render(parser, args.completions), end="")
        print(f"# Install: {install_hint(args.completions)}")
        return
    args.command = None
    if args.resume is not None:
        from pcode.sessions import is_session_selector

        if not is_session_selector(args.resume):
            # SESSION is optional, so argparse would otherwise swallow the first
            # word of `pcode -c fix the bug`. Anything unlike an ID is prompt text.
            args.prompt.insert(0, args.resume)
            args.resume = "latest"
    if args.prompt and args.prompt[0] == "config":
        args.command = "config"
        args.arguments = args.prompt[1:]
    args.prompt = " ".join(args.prompt).strip() or None
    if (args.profile_cpu or args.profile_memory) and args.profile is None:
        parser.error("--profile-cpu and --profile-memory require --profile DIR")
    if args.worktree and args.no_worktree:
        parser.error("--worktree and --no-worktree are mutually exclusive")
    with ExitStack() as stack:
        if args.profile is not None:
            from pcode.profiling import profile_session

            try:
                stack.enter_context(
                    profile_session(args.profile, cpu=args.profile_cpu, memory=args.profile_memory)
                )
            except (OSError, ValueError) as error:
                parser.error(
                    f"Cannot start profile ({type(error).__name__}); use a new writable DIR"
                )
        _run_cli(args, parser)


SESSION_WORKTREE_PREFIX = "pcode-"
WORKTREE_ACTIONS = {
    "status": "Branch, mainline, and what is unmerged",
    "merge": "Merge the mainline into this branch, then fast-forward the mainline",
    "resolve": "Ask the model to resolve the conflicts a merge stopped on",
    "finish": "Merge, remove the worktree and its branch, and quit",
    "remove": "Delete the merged worktree; the branch stays",
    "list": "Every worktree of this repository",
    "clean": "Delete every other worktree with nothing uncommitted or unmerged",
}


def _select_project_root(argv: list[str]) -> None:
    """Point preferences at `-C DIR` (else the cwd) ahead of full argument parsing."""
    from pcode.preferences import set_project_root

    root = Path.cwd()
    for index, arg in enumerate(argv):
        if arg in ("-C", "--workspace") and index + 1 < len(argv):
            root = Path(argv[index + 1])
        elif arg.startswith("--workspace="):
            root = Path(arg.partition("=")[2])
        elif arg.startswith("-C") and len(arg) > 2 and not arg.startswith("--"):
            root = Path(arg[2:])
    set_project_root(root if root.is_dir() else Path.cwd())


def _enter_worktree(workspace: Path, requested) -> tuple[Path, str | None]:
    """Create the session's worktree when asked to, returning (workspace, session id).

    `requested` is None (use the `worktree` setting), True (unnamed), or a
    name. Session worktrees are named `pcode-<name>` so `git worktree list`
    and `git branch` show which ones pcode made; unnamed ones use the session
    ID's prefix, so `pcode -c <prefix>` finds the session. Already inside a
    linked worktree, or outside git, the workspace is left alone rather than
    nested.
    """
    from uuid import uuid4

    from pcode import worktree

    if requested is None and load_preferences().get("worktree", "off") != "on":
        return workspace, None
    if worktree.main_checkout(workspace) is None:
        if requested is None:
            return workspace, None
        raise worktree.WorktreeError(
            f"--worktree needs a git repository; {workspace} is not in one."
        )
    if worktree.is_linked(workspace):
        return workspace, None
    identity = str(uuid4())
    name = requested if isinstance(requested, str) else identity[:8]
    created = worktree.create(workspace, SESSION_WORKTREE_PREFIX + name)
    try:
        worktree.run_setup(created, stream=sys.stderr)
    except worktree.WorktreeError:
        worktree.remove(created, force=True)
        raise
    print(f"worktree: {created.path} (branch {created.branch})", file=sys.stderr)
    return created.path, identity


def _leave_worktree_on_exit(app, ask=input, stream=None) -> None:
    """Tidy a session worktree on the way out, never losing work.

    Untouched (clean, nothing unmerged): removed with its branch, no question;
    a session that never had a turn is deleted too. Unmerged commits: per
    `worktree_exit`, ask (default yes), merge silently, or keep. Uncommitted
    changes, refusals, and hand-made worktrees (no `pcode-` prefix): kept, with
    a note on how to resume. `ask=None` means nobody is there to answer.
    """
    import shutil

    from pcode import worktree

    stream = stream or sys.stderr
    try:
        linked = worktree.describe(app.workspace)
        if linked is None:
            return
        ours = linked.branch.startswith(SESSION_WORKTREE_PREFIX)
        dirty = worktree.is_dirty(linked.path)
        unmerged = worktree.unmerged_commits(linked)
        untouched = ours and worktree.is_untouched(linked)
    except worktree.WorktreeError:
        return
    session = getattr(app.runtime, "session", None)
    resume = f"`pcode -C {linked.path} -c` resumes there"

    def repoint():
        if session is not None:
            session.info.workspace = str(linked.main)
            session.save_info()

    try:
        if untouched:
            worktree.remove(linked)
            worktree.delete_branch(linked)
            if session is not None and session.info.turns == 0:
                session.close()
                app.runtime.session = None
                shutil.rmtree(session.directory, ignore_errors=True)
            else:
                repoint()
            print(f"worktree: removed untouched {linked.path}", file=stream)
            return
        if dirty or not ours or not unmerged:
            state = "uncommitted changes" if dirty else f"{unmerged} unmerged commit(s)"
            print(f"worktree: {linked.path} ({linked.branch}) has {state}; {resume}", file=stream)
            return
        mode = load_preferences().get("worktree_exit", "ask")
        mainline = worktree.mainline_branch(linked.main)
        if mode == "ask" and ask is not None:
            print(
                f"worktree: {linked.branch} has {unmerged} commit(s) not in {mainline}.",
                file=stream,
            )
            try:
                answer = ask("Merge and remove the worktree? [Y/n] ").strip().lower()
            except (EOFError, OSError, KeyboardInterrupt):
                answer = "n"
            if answer not in ("", "y", "yes"):
                print(f"worktree: kept; {resume}", file=stream)
                return
        elif mode != "merge":
            print(
                f"worktree: {linked.path} ({linked.branch}) has {unmerged} unmerged commit(s); "
                f"{resume}",
                file=stream,
            )
            return
        print("worktree: " + worktree.finish(linked), file=stream)
        repoint()
    except (worktree.WorktreeError, OSError) as error:
        print(f"worktree: {error}\nworktree: kept; {resume}", file=stream)


def _run_cli(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.command == "config":
        try:
            print(configure(args.arguments))
        except (OSError, ValueError) as error:
            parser.exit(2, f"{error}\n")
        return
    if args.resume and (args.no_save or args.theme_preview):
        parser.error("--continue cannot be combined with --no-save or --theme-preview")
    if args.print:
        if args.theme_preview or args.sessions:
            parser.error("--print cannot be combined with --theme-preview or --sessions")
        if args.prompt is None:
            if sys.stdin.isatty():
                parser.error("--print needs a PROMPT argument or text on stdin")
            args.prompt = sys.stdin.read()
        if not args.prompt.strip():
            parser.error("--print needs a non-empty prompt")
    if args.sessions:
        from pcode.sessions import list_sessions

        console = Transcript(Console(), args.theme, color_style=args.color_style)
        records = list_sessions(args.session_dir)
        if not records:
            console.note("No saved sessions.")
        for info in records:
            console.note(f"{info.id}  {info.status}  {info.model}  {info.workspace}")
        return
    if args.theme_preview:
        # --theme-preview never constructs a provider, even when -m is also supplied.
        app = PreviewApp(theme=args.theme, color_style=args.color_style)
        app.transcript.welcome()
        # There is no mutable panel in the non-interactive sample.
        app.transcript.events(app.preview.demo(), show_tools=True)
        app.transcript.syntax_gallery()
        return
    if not args.print and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        parser.error("interactive mode needs a terminal; use --print PROMPT to answer without one")
    saved = None
    app = None
    try:
        if args.resume:
            from pcode.sessions import SavedSession, SessionError

            saved = SavedSession.open(args.resume, args.session_dir, args.workspace or Path.cwd())
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
        from pcode.preferences import rejected_project_keys, set_project_root

        set_project_root(workspace)
        if rejected := rejected_project_keys():
            print(
                f"pcode: ignoring user-only settings in .pcode/preferences.json: "
                f"{', '.join(rejected)} (set them with `pcode config set`)",
                file=sys.stderr,
            )
        from pcode.project_trust import prompt_trust

        # Before the worktree, whose setup script is one of the things being
        # trusted. --print has no one to ask, so untrusted code is skipped.
        prompt_trust(workspace, ask=None if args.print else input)
        session_id = None
        if not args.resume and not args.no_worktree and not args.theme_preview:
            workspace, session_id = _enter_worktree(workspace, args.worktree)
        app = PreviewApp(
            theme=args.theme,
            color_style=args.color_style,
            model=args.model,
            workspace=workspace,
            saved_session=saved,
            save=not args.no_save,
            session_dir=args.session_dir,
            resume=bool(args.resume),
            initial_prompt=args.prompt,
            session_id=session_id,
            # Keep stdout for the reply alone when printing.
            console=Console(stderr=True) if args.print else None,
        )
        if args.print:
            ok = app.run_print(args.prompt)
            _leave_worktree_on_exit(app, ask=None)
            if not ok:
                parser.exit(1)
        else:
            app.run()
            _leave_worktree_on_exit(app)
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
