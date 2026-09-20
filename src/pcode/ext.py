"""User extensions: Python files that add tools, hooks, instructions, and commands.

An extension is a module defining `setup(pcode)`, where `pcode` is an
`ExtensionAPI`. The model-facing surface (tools, instructions, lifecycle hooks)
is Pydantic AI's own capability system, so `add_capability` alone is complete;
the `tool`, `instructions`, and `hooks` helpers exist so common cases need no
knowledge of that class hierarchy. Commands and notices are pcode's.

Extensions run in-process with the user's permissions: the same trust boundary
as the shell tool. Project-local extensions load only for a repository the user
has trusted (`project_trust`), so cloning one cannot run its code at launch.

Loading happens with the rest of agent construction, off the terminal's startup
path. Every failure is recorded on the extension and reported, never raised: a
broken extension must not prevent a coding session.
"""

import importlib.util
import os
import re
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pcode.commands import Command
from pcode.preferences import SETTINGS, load_preferences, preferences_path

PROJECT_DIR = Path(".pcode") / "extensions"
# Defaults shipped with pcode, written against the same API as user extensions.
# Searched last, so a user or project file of the same name replaces one (an
# empty `setup` disables it). Keep this the only place they are special.
BUNDLED_DIR = Path(__file__).with_name("extensions")
ID_PREFIX = "ext."
# The authoring reference, shipped with the package so the model can read it
# with its file tools instead of the API being repeated in every prompt.
EXTENSION_GUIDE = Path(__file__).with_name("extension_guide.md")
Notify = Callable[[str, str], None]
LEVELS = ("info", "warning", "error")


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
    """What an extension may show the user. Bound to the terminal by the app."""

    def __init__(self, notify: Notify | None = None) -> None:
        self._notify = notify

    def notify(self, text: str, level: str = "info") -> None:
        """Print a transient notice in the transcript."""
        if level not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}")
        if self._notify is not None:
            self._notify(str(text), level)


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

    def register_command(
        self,
        name: str,
        description: str,
        handler: Callable[[str], None],
        *,
        arguments: tuple[str, ...] = (),
        aliases: tuple[str, ...] = (),
    ) -> None:
        """Add a slash command. `handler(argument)` runs on the terminal's event loop.

        Without `arguments`, any text is accepted; with them, only those values.
        Names taken by pcode itself are reported and skipped, never overridden.
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
    capabilities: list = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)

    @property
    def loaded(self) -> bool:
        return self.error is None

    def summary(self) -> str:
        counts = []
        if tools := sum(_tool_count(c) for c in self.capabilities):
            counts.append(f"{tools} tool{'s' if tools != 1 else ''}")
        if hooks := [c for c in self.capabilities if type(c).__name__ == "Hooks"]:
            counts.append(f"{sum(len(h._registry) for h in hooks)} hooks")
        if self.commands:
            counts.append(", ".join(c.name for c in self.commands))
        return ", ".join(counts) or "no contributions"


def _tool_count(capability) -> int:
    toolset = capability.get_toolset() if hasattr(capability, "get_toolset") else None
    # Native-or-local capabilities wrap their function toolset in a prepared one.
    while toolset is not None and not hasattr(toolset, "tools"):
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


def load_extension(extension: Extension, workspace: Path, ui: ExtensionUI) -> Extension:
    """Import the module, run `setup`, and validate what it contributed."""
    extension.error = None
    extension.capabilities = []
    extension.commands = []
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
    def failed(self) -> list[Extension]:
        return [extension for extension in self.extensions if not extension.loaded]

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
            if extension.loaded:
                lines.append(f"{extension.name} ({shown}): {extension.summary()}")
            else:
                lines.append(f"{extension.name} ({shown}): failed, {extension.error}")
        return lines


def load_extensions(workspace: Path, ui: ExtensionUI | None = None) -> LoadedExtensions:
    """Discover and load every extension; failures are recorded, not raised."""
    workspace = workspace.resolve()
    ui = ui or ExtensionUI()
    return LoadedExtensions(
        [load_extension(extension, workspace, ui) for extension in discover_extensions(workspace)]
    )
