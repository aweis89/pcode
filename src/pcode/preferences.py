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

    def validate(self, key: str, value: str) -> None:
        if self.choices:
            if value not in self.choices:
                raise ValueError(f"{key} must be one of: {', '.join(self.choices)}")
        elif not value or any(char.isspace() for char in value):
            raise ValueError(f"{key} must be a non-empty model name without whitespace.")


SETTINGS = {
    "theme": Setting("dark", ("dark", "light")),
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


def apply_effort(agent, model: str, effort: str | None) -> None:
    if effort not in EFFORTS or model.split(":", 1)[0] not in OPENAI_PROVIDERS:
        return
    settings = dict(agent.model_settings or {})
    if effort == "default":
        settings.pop("openai_reasoning_effort", None)
    else:
        settings["openai_reasoning_effort"] = effort
    agent.model_settings = settings
