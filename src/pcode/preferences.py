"""Non-secret, user-level defaults, separate from conversation storage."""

import json
import os
import tempfile
from pathlib import Path

EFFORTS = ("low", "medium", "high", "xhigh", "default")
OPENAI_PROVIDERS = ("openai", "openai-chat", "openai-responses", "openai-codex")


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
    if isinstance(data.get("model"), str) and data["model"].strip():
        result["model"] = data["model"]
    if data.get("effort") in EFFORTS:
        result["effort"] = data["effort"]
    return result


def save_preferences(**updates: str) -> None:
    data = {**load_preferences(), **updates}
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
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
