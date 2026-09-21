"""Non-secret, user-level defaults, separate from conversation storage."""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock
from pygments.styles import get_all_styles

# Every Pygments style installed here, including any added by a plugin package.
# The scan costs a few milliseconds once; Rich imports Pygments regardless.
SYNTAX_THEMES = tuple(sorted(get_all_styles()))

EFFORTS = ("low", "medium", "high", "xhigh", "default")
OPENAI_PROVIDERS = ("openai", "openai-chat", "openai-responses", "openai-codex")


@dataclass(frozen=True)
class Setting:
    default: str | None
    choices: tuple[str, ...] = ()
    positive_integer: bool = False
    # A count whose zero means "off", so it cannot reuse positive_integer's floor.
    whole_number: bool = False
    # An os.pathsep-separated list of directories; empty means "none".
    path_list: bool = False
    # A comma-separated list of extension names; empty means "none".
    name_list: bool = False
    # One line shown beside the key in /config completions.
    description: str = ""

    def validate(self, key: str, value: str) -> None:
        if self.path_list:
            if value and any(not entry.strip() for entry in value.split(os.pathsep)):
                raise ValueError(f"{key} must be directories separated by '{os.pathsep}'.")
        elif self.name_list:
            if value and any(
                not entry.strip() or any(char.isspace() for char in entry.strip())
                for entry in value.split(",")
            ):
                raise ValueError(f"{key} must be comma-separated names without whitespace.")
        elif self.positive_integer or self.whole_number:
            floor = 0 if self.whole_number else 1
            if not value.isascii() or not value.isdecimal() or int(value) < floor:
                raise ValueError(
                    f"{key} must be a whole number (0 or more)."
                    if self.whole_number
                    else f"{key} must be a positive integer."
                )
        elif self.choices:
            if value not in self.choices:
                raise ValueError(f"{key} must be one of: {', '.join(self.choices)}")
        elif not value or any(char.isspace() for char in value):
            raise ValueError(f"{key} must be a non-empty model name without whitespace.")


SEND_MODES = ("steering", "queue", "interrupt")
ANTHROPIC_AUTH_SOURCES = ("api-key", "oauth")

# A user-level directory shared across workspaces, plus the workspace's own
# `.agents/skills`, which the asset-root scan already covers but which belongs in
# the listed default so replacing the list is an informed choice.
DEFAULT_SKILL_DIRS = os.pathsep.join(("~/.agents/skills", ".agents/skills"))


SETTINGS = {
    "send_mode": Setting(
        "steering",
        SEND_MODES,
        description="Enter mid-turn: steer the running turn, queue for after, or interrupt",
    ),
    # PCODE_ANTHROPIC_AUTH still wins over the saved choice.
    "anthropic_auth": Setting(
        None,
        ANTHROPIC_AUTH_SOURCES,
        description="Anthropic credential /login selected; env PCODE_ANTHROPIC_AUTH overrides",
    ),
    "meridian_managed": Setting(
        "off",
        ("on", "off"),
        description="Launch a process-owned Meridian instance instead of using a shared one",
    ),
    "repo_context_walk_up": Setting(
        "on",
        ("on", "off"),
        description="Also load AGENTS.md files from directories above the workspace",
    ),
    "repo_context_nested": Setting(
        "off",
        ("off", "pointer", "contents"),
        description="Nested AGENTS.md found while reading: ignore, mention its path, or inject it",
    ),
    "skill_commands": Setting(
        "prefix",
        ("prefix", "bare", "both", "off"),
        description="SKILL.md assets as slash commands: /skill:NAME, /NAME, both, or none",
    ),
    # Relative entries resolve against the workspace; `~` expands to the user's home.
    "skill_dirs": Setting(
        DEFAULT_SKILL_DIRS,
        path_list=True,
        description=f"Extra skill directories, '{os.pathsep}'-separated, after workspace roots",
    ),
    # Extensions run arbitrary Python at launch, so a workspace's `.pcode/extensions`
    # is opt-in; the user-level directory beside preferences.json always loads.
    # `on` trusts every repository; `trusted_projects` is the per-repository grant
    # the launch prompt appends to (primary checkout paths).
    "project_extensions": Setting(
        "off",
        ("on", "off"),
        description="Load every workspace's .pcode/extensions without asking (runs code at launch)",
    ),
    "trusted_projects": Setting(
        "",
        path_list=True,
        description="Checkouts whose .pcode/extensions load without asking (launch prompt appends)",
    ),
    "extension_dirs": Setting(
        "",
        path_list=True,
        description=f"Extra extension directories, '{os.pathsep}'-separated, after the user one",
    ),
    # Which discovered extensions run, by name. An extension loads unless it is
    # named in `extensions_off`, or declares `DEFAULT_ENABLED = False` and is not
    # named in `extensions_on`. `/extensions on|off NAME` writes both.
    "extensions_off": Setting(
        "",
        name_list=True,
        description="Extensions never loaded, comma-separated (/extensions off NAME)",
    ),
    "extensions_on": Setting(
        "",
        name_list=True,
        description="Opt-in extensions to load, comma-separated (/extensions on NAME)",
    ),
    # `--worktree [NAME]` and `--no-worktree` override per run.
    "worktree": Setting(
        "off",
        ("on", "off"),
        description="Start each session in its own .worktrees/<session> git worktree",
    ),
    # An untouched worktree is always removed; uncommitted changes are always kept.
    "worktree_exit": Setting(
        "ask",
        ("ask", "merge", "keep"),
        description="Worktree with unmerged commits on exit: ask, merge and remove, or keep",
    ),
    "retry_attempts": Setting(
        "1",
        whole_number=True,
        description="Automatic retries after a dropped connection; 0 disables",
    ),
    # Pydantic AI's default of 1 ends the turn on a second malformed call, which a
    # long `replacements` array can hit by itself.
    "tool_retries": Setting(
        "3",
        whole_number=True,
        description="Corrections offered to the model per turn when a tool call fails validation",
    ),
    "error_scrollback_lines": Setting(
        "20",
        positive_integer=True,
        description="Lines of an error notice kept in scrollback before it is clipped",
    ),
    "tool_error_scrollback": Setting(
        "off",
        ("on", "off"),
        description="Keep a failed tool call's full diagnostic in scrollback, not one line",
    ),
    "regenerate_on_resize": Setting(
        "on",
        ("on", "off"),
        description="Rebuild scrollback at the new width when the terminal resizes",
    ),
    "show_edits": Setting(
        "on",
        ("on", "off"),
        description="Show a diff preview of each file edit in the transcript",
    ),
    "show_commands": Setting(
        "off",
        ("on", "off"),
        description="Mirror shell command output into scrollback (Ctrl+G toggles the live preview)",
    ),
    "command_scrollback_lines": Setting(
        "20",
        positive_integer=True,
        description="Lines of shell output mirrored into scrollback per command",
    ),
    # The pinned live preview competes with the editor for screen space, so it
    # caps separately from the scrollback mirror.
    "command_preview_lines": Setting(
        "10",
        positive_integer=True,
        description="Lines of the pinned live preview of a running shell command",
    ),
    "show_tasks": Setting(
        "on", ("on", "off"), description="Show the model's plan as a pinned task list"
    ),
    "autohide_tasks": Setting(
        "on", ("on", "off"), description="Hide the task list once every step is done"
    ),
    "show_thinking": Setting(
        "off", ("on", "off"), description="Stream the model's thinking into the transcript"
    ),
    "editing_mode": Setting(
        "emacs", ("emacs", "vi"), description="Key bindings for the prompt editor"
    ),
    "theme": Setting(
        "dark",
        ("dark", "light", "auto"),
        description="Palette for the terminal background; auto detects it",
    ),
    # Chosen per palette so `theme auto` keeps highlighting legible on either
    # background. `/colors terminal` overrides both with the ANSI styles.
    "syntax_dark": Setting(
        "gruvbox-dark",
        SYNTAX_THEMES,
        description="Pygments style for fenced code on the dark palette (/theme previews)",
    ),
    "syntax_light": Setting(
        "gruvbox-light",
        SYNTAX_THEMES,
        description="Pygments style for fenced code on the light palette (/theme previews)",
    ),
    "autocompact": Setting(
        "off",
        ("on", "off"),
        description="Compact the conversation automatically as the context window fills",
    ),
    # Edits, plans, shell, and delegation stay native so their transcript display survives.
    "code_mode": Setting(
        "off",
        ("on", "off"),
        description="Batch read-only tool calls through one sandboxed run_code snippet",
    ),
    # Read by the bundled web_research extension. Applies on /reload or next launch.
    "web_search": Setting(
        "auto",
        ("auto", "local", "off"),
        description="Web tools: provider-native when available, local-only, or none",
    ),
    "tool_output_mode": Setting(
        "spill",
        ("spill", "truncate", "off"),
        description="Large tool results: spill to a file with a handle, truncate, or keep whole",
    ),
    "tool_output_threshold": Setting(
        "10000",
        positive_integer=True,
        description="Characters a tool result may have before it is spilled or truncated",
    ),
    "tool_output_preview_chars": Setting(
        "1000",
        positive_integer=True,
        description="Characters of a spilled result shown inline beside its handle",
    ),
    "tool_output_max_chars": Setting(
        "4000",
        positive_integer=True,
        description="Characters kept when a tool result is truncated",
    ),
    "tool_output_strategy": Setting(
        "head_tail",
        ("head", "tail", "head_tail"),
        description="Which part of a truncated tool result survives",
    ),
    "tool_output_retention_hours": Setting(
        "0",
        whole_number=True,
        description="Hours to keep spilled results on disk; 0 keeps them forever",
    ),
    "effort": Setting(
        "default", EFFORTS, description="Default reasoning effort for models /effort has not set"
    ),
    "model": Setting(None, description="Model name; unset uses the offline preview"),
}


# Settings a repository must not be able to set for whoever clones it: each
# either runs code on launch or chooses which credential or local process to
# use. They are read from the user file only; a project file setting them is
# reported by `rejected_project_keys` and otherwise ignored.
USER_ONLY = frozenset(
    {
        "project_extensions",
        "trusted_projects",
        "extension_dirs",
        "extensions_off",
        "extensions_on",
        "meridian_managed",
        "anthropic_auth",
    }
)

PROJECT_PREFERENCES = Path(".pcode") / "preferences.json"

# The launch workspace, fixed once by the CLI so every later load_preferences()
# call sees the same overlay. Not the current directory: `-C` and worktrees
# make those differ, and the worktree carries the same committed file anyway.
_project_root: Path | None = None


def set_project_root(path: Path | None) -> None:
    global _project_root
    _project_root = path.resolve() if path is not None else None


def config_dir() -> Path:
    """pcode's own config directory: preferences, credentials, mcp.json, extensions.

    `PCODE_CONFIG_DIR` names it directly so several independent instances (each
    with its own login and settings) can coexist without moving every other
    XDG-aware program along with it; otherwise `$XDG_CONFIG_HOME/pcode`.
    """
    override = os.environ.get("PCODE_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "pcode"


def preferences_path() -> Path:
    return config_dir() / "preferences.json"


def project_preferences_path() -> Path | None:
    """The workspace's overlay file, or None before the CLI has named a workspace."""
    return _project_root / PROJECT_PREFERENCES if _project_root is not None else None


def _read_valid(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, setting in SETTINGS.items():
        value = data.get(key)
        if not isinstance(value, str):
            continue
        try:
            setting.validate(key, value)
        except ValueError:
            continue
        result[key] = value
    return result


def load_preferences() -> dict[str, str]:
    """User defaults with the workspace's `.pcode/preferences.json` layered on top."""
    merged = _read_valid(preferences_path())
    for key, value in _read_valid(project_preferences_path()).items():
        if key not in USER_ONLY:
            merged[key] = value
    return merged


def rejected_project_keys() -> list[str]:
    """User-only keys the project file tries to set, for a launch warning."""
    path = project_preferences_path()
    if path is None:
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return sorted(USER_ONLY & set(data)) if isinstance(data, dict) else []


def read_preferences(path: Path | None = None) -> dict:
    """Read without discarding unknown keys or silently repairing a broken file."""
    path = path or preferences_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except ValueError:
        raise ValueError(
            f"Invalid preferences JSON: {path}. Repair the file before editing."
        ) from None
    if not isinstance(data, dict):
        raise ValueError(f"Preferences must be a JSON object: {path}")
    return data


def save_preferences(**updates: str) -> None:
    update_preferences(updates)


def update_preferences(
    updates: dict[str, str], *, remove: tuple[str, ...] = (), path: Path | None = None
) -> None:
    path = path or preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize read/modify/write across terminals, including legacy shortcuts.
    with FileLock(str(path) + ".lock", timeout=5):
        data = read_preferences(path)
        for key in remove:
            data.pop(key, None)
        data.update(updates)
        _write_preferences(path, data)


def _write_preferences(path: Path, data: dict) -> None:
    # Atomic replacement avoids leaving a partial file after an interrupted write.
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
            name = file.name
            json.dump(data, file, indent=2)
            file.write("\n")
        os.replace(name, path)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


# Per-model effort lives outside SETTINGS: it is a mapping, not a string, and
# the `effort` setting stays as the fallback for models never chosen explicitly.
MODEL_EFFORTS_KEY = "model_efforts"


def _model_efforts(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    stored = data.get(MODEL_EFFORTS_KEY) if isinstance(data, dict) else None
    if not isinstance(stored, dict):
        return {}
    return {
        model: effort
        for model, effort in stored.items()
        if isinstance(model, str) and effort in EFFORTS
    }


def model_efforts() -> dict[str, str]:
    """Saved effort per model, with the workspace overlay layered on top."""
    merged = _model_efforts(preferences_path())
    merged.update(_model_efforts(project_preferences_path()))
    return merged


def effort_for(model: str | None) -> str | None:
    """The effort to request for `model`: its own, else the shared default."""
    saved = model_efforts().get(model or "")
    return saved if saved is not None else load_preferences().get("effort")


def save_model_effort(model: str, effort: str) -> None:
    """Record `effort` for `model` alone, leaving other models untouched."""
    with FileLock(str(preferences_path()) + ".lock", timeout=5):
        path = preferences_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = read_preferences(path)
        stored = data.get(MODEL_EFFORTS_KEY)
        stored = dict(stored) if isinstance(stored, dict) else {}
        stored[model] = effort
        data[MODEL_EFFORTS_KEY] = stored
        _write_preferences(path, data)


def effort_setting(model: str | None) -> str | None:
    provider = (model or "").split(":", 1)[0]
    if provider in OPENAI_PROVIDERS:
        return "openai_reasoning_effort"
    if provider in ("anthropic", "meridian"):
        return "anthropic_effort"
    return None


def apply_effort(agent, model: str, effort: str | None) -> None:
    key = effort_setting(model)
    if effort not in EFFORTS or key is None:
        return
    settings = dict(agent.model_settings or {})
    if effort == "default":
        settings.pop(key, None)
    else:
        # Older Claude models call their highest effort "max", not "xhigh".
        profile = getattr(getattr(agent, "model", None), "profile", {}) or {}
        if key == "anthropic_effort" and effort == "xhigh":
            effort = "xhigh" if profile.get("anthropic_supports_xhigh_effort") else "max"
        settings[key] = effort
    agent.model_settings = settings


def apply_thinking(agent, model: str, shown: bool) -> None:
    """Request visible Anthropic thinking on future turns, not just a UI preview."""
    if not model.startswith("anthropic:"):
        return
    current = getattr(agent, "model_settings", None)
    if not shown and not current:
        return
    settings = dict(current or {})
    if shown:
        from pydantic_ai.profiles.anthropic import anthropic_model_profile

        # Before /login, Agent.model can still be an unresolved string. Looking
        # up its profile must not force credential loading just to open the UI.
        profile = getattr(agent.model, "profile", None)
        if profile is None:
            profile = anthropic_model_profile(model.removeprefix("anthropic:")) or {}
        settings["anthropic_thinking"] = (
            {"type": "adaptive", "display": "summarized"}
            if profile.get("anthropic_supports_adaptive_thinking")
            else {"type": "enabled", "budget_tokens": 2048, "display": "summarized"}
        )
        # The legacy budget is below the adapter's default max_tokens (4096).
        # Do not change effort or output limits as a side effect of visibility.
    else:
        settings.pop("anthropic_thinking", None)
    # Replace instead of mutating settings captured by an in-flight run.
    agent.model_settings = settings or None
