"""Non-secret, user-level defaults, separate from conversation storage."""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock

EFFORTS = ("low", "medium", "high", "xhigh", "default")
OPENAI_PROVIDERS = ("openai", "openai-chat", "openai-responses", "openai-codex")


@dataclass(frozen=True)
class Setting:
    default: str | None
    choices: tuple[str, ...] = ()
    positive_integer: bool = False

    def validate(self, key: str, value: str) -> None:
        if self.positive_integer:
            if not value.isascii() or not value.isdecimal() or int(value) < 1:
                raise ValueError(f"{key} must be a positive integer.")
        elif self.choices:
            if value not in self.choices:
                raise ValueError(f"{key} must be one of: {', '.join(self.choices)}")
        elif not value or any(char.isspace() for char in value):
            raise ValueError(f"{key} must be a non-empty model name without whitespace.")


SEND_MODES = ("steering", "queue", "interrupt")


SETTINGS = {
    "send_mode": Setting("steering", SEND_MODES),
    "meridian_managed": Setting("off", ("on", "off")),
    "error_scrollback_lines": Setting("20", positive_integer=True),
    "regenerate_on_resize": Setting("on", ("on", "off")),
    "command_scrollback": Setting("off", ("on", "off")),
    "command_scrollback_lines": Setting("20", positive_integer=True),
    "show_tasks": Setting("on", ("on", "off")),
    "autohide_tasks": Setting("on", ("on", "off")),
    "show_thinking": Setting("off", ("on", "off")),
    "editing_mode": Setting("emacs", ("emacs", "vi")),
    "theme": Setting("dark", ("dark", "light", "auto")),
    "autocompact": Setting("off", ("on", "off")),
    "effort": Setting("default", EFFORTS),
    "model": Setting(None),
}


def preferences_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "pcode" / "preferences.json"


def load_preferences() -> dict[str, str]:
    try:
        data = json.loads(preferences_path().read_text())
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


def read_preferences() -> dict:
    """Read without discarding unknown keys or silently repairing a broken file."""
    path = preferences_path()
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


def update_preferences(updates: dict[str, str], *, remove: tuple[str, ...] = ()) -> None:
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize read/modify/write across terminals, including legacy shortcuts.
    with FileLock(str(path) + ".lock", timeout=5):
        data = read_preferences()
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
