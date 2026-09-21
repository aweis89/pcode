"""User extensions: Python files that add tools, hooks, instructions, and commands.

An extension is a module defining `setup(pcode)`, where `pcode` is an
`ExtensionAPI`. The model-facing surface (tools, instructions, lifecycle hooks)
is Pydantic AI's own capability system, so `add_capability` alone is complete;
the `tool`, `instructions`, and `hooks` helpers exist so common cases need no
knowledge of that class hierarchy. Commands and notices are pcode's.

Extensions run in-process with the user's permissions: the same trust boundary
as the shell tool. Project-local extensions load only for a repository the user
has trusted (`project_trust`), so cloning one cannot run its code at launch.

A discovered extension runs unless the user turned it off (`extensions_off`), or
it declares `DEFAULT_ENABLED = False` and the user has not turned it on
(`extensions_on`). That is how a bundled default ships opt-in. `/extensions`
lists the state and writes both preferences.

Loading happens with the rest of agent construction, off the terminal's startup
path. Every failure is recorded on the extension and reported, never raised: a
broken extension must not prevent a coding session.
"""

import importlib.util
import os
import re
import sys
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from pcode.commands import Command
from pcode.preferences import SETTINGS, load_preferences, preferences_path, update_preferences

PROJECT_DIR = Path(".pcode") / "extensions"
# Defaults shipped with pcode, written against the same API as user extensions.
# Searched last, so a user or project file of the same name replaces one (an
# empty `setup` disables it). Keep this the only place they are special.
BUNDLED_DIR = Path(__file__).with_name("extensions")
ID_PREFIX = "ext."
# A module declaring `DEFAULT_ENABLED = False` is opt-in: discovered and listed,
# but `setup` runs only once its name is in `extensions_on`.
DEFAULT_FLAG = "DEFAULT_ENABLED"
# The authoring reference, shipped with the package so the model can read it
# with its file tools instead of the API being repeated in every prompt.
EXTENSION_GUIDE = Path(__file__).with_name("extension_guide.md")
Notify = Callable[[str, str], None]
LEVELS = ("info", "warning", "error")


def name_list(key: str) -> set[str]:
    """A comma-separated preference read as a set of extension names."""
    value = load_preferences().get(key, SETTINGS[key].default) or ""
    return {entry.strip() for entry in value.split(",") if entry.strip()}


def set_enabled(name: str, enabled: bool) -> None:
    """Record that `name` should (not) load, clearing the opposite list.

    Both lists are written because "on" must beat a `DEFAULT_ENABLED = False`
    module *and* an earlier "off", and neither knows which applied.
    """
    off, on = name_list("extensions_off"), name_list("extensions_on")
    off, on = (off - {name}, on | {name}) if enabled else (off | {name}, on - {name})
    values = {"extensions_off": ",".join(sorted(off)), "extensions_on": ",".join(sorted(on))}
    # An empty list is the default, so drop the key rather than saving "".
    update_preferences(
        {key: value for key, value in values.items() if value},
        remove=tuple(key for key, value in values.items() if not value),
    )


def user_extension_dir() -> Path:
    """The always-searched user directory, beside preferences.json."""
    return preferences_path().parent / "extensions"


def extension_dirs(workspace: Path) -> list[tuple[Path, str]]:
    """Resolve the searched directories with their scope, first match winning."""
    from pcode.project_trust import is_trusted

    preferences = load_preferences()
    directories: list[tuple[Path, str]] = []
    if is_trusted(workspace):
        directories.append((workspace / PROJECT_DIR, "project"))
    directories.append((user_extension_dir(), "user"))
    configured = preferences.get("extension_dirs", SETTINGS["extension_dirs"].default) or ""
    for entry in configured.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        path = Path(entry).expanduser()
        directories.append((path if path.is_absolute() else workspace / path, "configured"))
    directories.append((BUNDLED_DIR, "bundled"))
    return directories


def discover_extensions(workspace: Path) -> list["Extension"]:
    """Locate `*.py` files and `*/__init__.py` packages, keeping the first of each name."""
    found: dict[str, Extension] = {}
    for directory, scope in extension_dirs(workspace.resolve()):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.name.startswith(("_", ".")):
                continue
            if path.is_file() and path.suffix == ".py":
                name = path.stem
            elif path.is_dir() and (path / "__init__.py").is_file():
                name = path.name
            else:
                continue
            if name not in found:
                found[name] = Extension(name, path, scope)
    return list(found.values())


class ExtensionUI:
    """What an extension may ask of the terminal. Bound to it by the app."""

    def __init__(
        self, notify: Notify | None = None, request_reload: Callable[[], None] | None = None
    ) -> None:
        self._notify = notify
        self._request_reload = request_reload

    def notify(self, text: str, level: str = "info") -> None:
        """Print a transient notice in the transcript."""
        if level not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}")
        if self._notify is not None:
            self._notify(str(text), level)

    def request_reload(self) -> None:
        """Ask for `/reload` once the terminal is idle.

        For an extension whose contributions depend on state a command just
        changed: `setup` runs again and the agent is rebuilt around the same
        conversation. Raises `ValueError` when a turn is in progress.
        """
        if self._request_reload is not None:
            self._request_reload()


class ExtensionAPI:
    """The object handed to `setup`. Collects contributions; `capabilities()` builds them."""

    def __init__(self, name: str, workspace: Path, ui: ExtensionUI) -> None:
        self.name = name
        self.workspace = workspace
        self.ui = ui
        self.id = ID_PREFIX + name
        self._tools: list = []
        self._instructions: list[str] = []
        self._capabilities: list = []
        self._hooks = None
        self.commands: list[Command] = []
        self.subagents: list = []
        self.closers: list[Callable[[], Awaitable[None]]] = []

    def tool(self, function):
        """Register a plain function as a model-callable tool (decorator).

        The docstring becomes the description and type hints the schema, as
        with Pydantic AI's `Agent.tool_plain`.
        """
        self._tools.append(function)
        return function

    def instructions(self, text: str) -> None:
        """Add static system-prompt text. Keep it fixed: it is part of the cached prefix."""
        self._instructions.append(text)

    @property
    def hooks(self):
        """Lifecycle hooks: `@pcode.hooks.on.before_tool_execute`, `after_model_request`, ...

        Names are Pydantic AI's `Hooks` decorators; raise `ModelRetry` from a
        `before_tool_execute` hook to block a call and tell the model why.
        """
        if self._hooks is None:
            from pydantic_ai.capabilities import Hooks

            self._hooks = Hooks(id=self.id + ".hooks")
        return self._hooks

    def add_capability(self, capability) -> None:
        """Add any Pydantic AI capability; the escape hatch for everything else."""
        if getattr(capability, "id", None) is None:
            try:
                capability.id = self.id
            except (AttributeError, TypeError):
                pass  # Frozen or slotted: it stays anonymous in /status.
        self._capabilities.append(capability)

    def subagent(self, agent, **options) -> None:
        """Offer `agent` to the model through `delegate_task`, beside the explorer.

        `agent` is a Pydantic AI `Agent` with a `name` and `description`; leave
        its model unset to run on the session's model. `options` are the
        Harness `SubAgent` fields (`usage_limits`, `timeout_seconds`, ...).
        """
        from pydantic_ai_harness.subagents import SubAgent

        self.subagents.append(SubAgent(agent, **options))

    def on_close(self, function: Callable[[], Awaitable[None]]):
        """Run `await function()` when the terminal exits, for resources a tool started."""
        self.closers.append(function)
        return function

    def register_command(
        self,
        name: str,
        description: str,
        handler: Callable[[str], None],
        *,
        arguments: tuple[str, ...] = (),
        aliases: tuple[str, ...] = (),
        argument_descriptions: dict[str, str] | None = None,
    ) -> None:
        """Add a slash command. `handler(argument)` runs on the terminal's event loop.

        Without `arguments`, any text is accepted; with them, only those values,
        and `argument_descriptions` labels each in the completion menu. Names
        taken by pcode itself are reported and skipped, never overridden.
        """
        if not name.startswith("/"):
            name = "/" + name
        self.commands.append(
            Command(
                name,
                description,
                handler,
                arguments=arguments,
                aliases=aliases,
                free_arguments=not arguments,
                group="Extensions",
                argument_descriptions=argument_descriptions,
            )
        )

    def capabilities(self) -> list:
        """Everything this extension contributes to the agent."""
        result = list(self._capabilities)
        if self._tools or self._instructions:
            from pydantic_ai.capabilities import Capability

            result.append(
                Capability(
                    id=self.id,
                    tools=self._tools,
                    instructions="\n\n".join(self._instructions) or None,
                )
            )
        if self._hooks is not None and self._hooks._registry:
            result.append(self._hooks)
        return result


@dataclass
class Extension:
    """One discovered extension and the outcome of loading it."""

    name: str
    path: Path
    scope: str
    error: str | None = None
    # Why `setup` was not run: the user turned it off, or it ships opt-in.
    disabled: str | None = None
    capabilities: list = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)
    subagents: list = field(default_factory=list)
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.disabled is None

    @property
    def loaded(self) -> bool:
        return self.enabled and self.error is None

    def state(self) -> str:
        """One phrase for the report: what it contributed, or why it did not."""
        if self.disabled is not None:
            return f"{self.disabled} (/extensions on {self.name})"
        if self.error is not None:
            return f"failed, {self.error}"
        return self.summary()

    def summary(self) -> str:
        counts = []
        if tools := sum(_tool_count(c) for c in self.capabilities):
            counts.append(f"{tools} tool{'s' if tools != 1 else ''}")
        if hooks := [c for c in self.capabilities if type(c).__name__ == "Hooks"]:
            counts.append(f"{sum(len(h._registry) for h in hooks)} hooks")
        if self.subagents:
            counts.append(", ".join(f"@{s.resolved_name}" for s in self.subagents))
        if self.commands:
            counts.append(", ".join(c.name for c in self.commands))
        return ", ".join(counts) or "no contributions"


def _tool_count(capability) -> int:
    return _toolset_size(capability.get_toolset() if hasattr(capability, "get_toolset") else None)


def _toolset_size(toolset) -> int:
    # Native-or-local capabilities wrap their function toolset in a prepared one;
    # a capability given both `tools` and `toolsets` combines them.
    while toolset is not None and not hasattr(toolset, "tools"):
        if (parts := getattr(toolset, "toolsets", None)) is not None:
            return sum(_toolset_size(part) for part in parts)
        toolset = getattr(toolset, "wrapped", None)
    tools = getattr(toolset, "tools", None)
    return len(tools) if isinstance(tools, dict) else 0


def _module_name(extension: Extension) -> str:
    return "pcode_ext_" + re.sub(r"\W", "_", extension.name)


def _failure(error: BaseException, path: Path) -> str:
    """One line naming the error and, when it is in the extension, the offending line."""
    message = f"{type(error).__name__}: {error}"
    for frame in reversed(traceback.extract_tb(error.__traceback__)):
        if Path(frame.filename).resolve().is_relative_to(path.resolve().parent):
            return f"{message} ({Path(frame.filename).name}:{frame.lineno})"
    return message


def load_extension(
    extension: Extension,
    workspace: Path,
    ui: ExtensionUI,
    off: set[str] | None = None,
    on: set[str] | None = None,
) -> Extension:
    """Import the module, run `setup`, and validate what it contributed.

    An extension the user turned off is never imported; one that ships opt-in is
    imported (that is where the flag lives) but its `setup` does not run.
    """
    extension.error = None
    extension.disabled = None
    extension.capabilities = []
    extension.commands = []
    extension.subagents = []
    extension.closers = []
    off = name_list("extensions_off") if off is None else off
    on = name_list("extensions_on") if on is None else on
    if extension.name in off:
        extension.disabled = "off"
        return extension
    target = extension.path / "__init__.py" if extension.path.is_dir() else extension.path
    name = _module_name(extension)
    try:
        spec = importlib.util.spec_from_file_location(
            name,
            target,
            submodule_search_locations=[str(extension.path)] if extension.path.is_dir() else None,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {target}")
        module = importlib.util.module_from_spec(spec)
        # Registered first so dataclasses and pickling inside the module resolve.
        sys.modules[name] = module
        spec.loader.exec_module(module)
        if not getattr(module, DEFAULT_FLAG, True) and extension.name not in on:
            extension.disabled = "off by default"
            sys.modules.pop(name, None)
            return extension
        setup = getattr(module, "setup", None)
        if not callable(setup):
            raise AttributeError("extension defines no setup(pcode) function")
        api = ExtensionAPI(extension.name, workspace, ui)
        setup(api)
        capabilities = api.capabilities()
        for capability in capabilities:
            # Exercise the static getters now, the way Harness validates authored
            # capabilities, so a broken tool schema fails at load rather than mid-turn.
            capability.get_instructions()
            capability.get_toolset()
        extension.capabilities = capabilities
        extension.commands = api.commands
        extension.subagents = api.subagents
        extension.closers = api.closers
    except BaseException as error:  # noqa: BLE001 - a bad extension must not stop launch.
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        extension.error = _failure(error, extension.path)
        sys.modules.pop(name, None)
    return extension


@dataclass
class LoadedExtensions:
    """The result of one discovery-and-load pass."""

    extensions: list[Extension] = field(default_factory=list)

    @property
    def capabilities(self) -> list:
        return [c for extension in self.extensions for c in extension.capabilities]

    @property
    def commands(self) -> list[Command]:
        return [c for extension in self.extensions for c in extension.commands]

    @property
    def subagents(self) -> list:
        return [s for extension in self.extensions for s in extension.subagents]

    @property
    def failed(self) -> list[Extension]:
        return [extension for extension in self.extensions if extension.error is not None]

    @property
    def disabled(self) -> list[Extension]:
        return [extension for extension in self.extensions if not extension.enabled]

    async def close(self) -> None:
        """Run every extension's close hooks; one failing does not skip the rest."""
        for extension in self.extensions:
            for closer in extension.closers:
                try:
                    await closer()
                except Exception as error:  # noqa: BLE001 - exit must not stall on an extension.
                    print(f"Extension {extension.name} failed to close: {error}", file=sys.stderr)

    def report(self, workspace: Path) -> list[str]:
        """Human lines for /extensions and the startup notice."""
        if not self.extensions:
            return []
        lines = []
        for extension in self.extensions:
            path = extension.path
            shown = (
                "bundled"
                if extension.scope == "bundled"
                else path.relative_to(workspace).as_posix()
                if path.is_relative_to(workspace)
                else path
            )
            lines.append(f"{extension.name} ({shown}): {extension.state()}")
        return lines


def load_extensions(workspace: Path, ui: ExtensionUI | None = None) -> LoadedExtensions:
    """Discover and load every extension; failures are recorded, not raised."""
    workspace = workspace.resolve()
    ui = ui or ExtensionUI()
    off, on = name_list("extensions_off"), name_list("extensions_on")
    return LoadedExtensions(
        [
            load_extension(extension, workspace, ui, off, on)
            for extension in discover_extensions(workspace)
        ]
    )
