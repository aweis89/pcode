"""Global defaults commands, shared by the CLI and interactive terminal.

Editing defaults deliberately does not mutate a running conversation. In-session
shortcuts remain the way to change both the active setting and its saved default.
"""

import json
from collections.abc import Sequence

from pcode.preferences import (
    SETTINGS,
    Setting,
    preferences_path,
    read_preferences,
    update_preferences,
)

USAGE = "config [list | path | get KEY | set KEY VALUE | unset KEY]"


def _setting(key: str) -> Setting:
    if key not in SETTINGS:
        raise ValueError(f"Unknown setting '{key}'. Available: {', '.join(SETTINGS)}")
    return SETTINGS[key]


def _effective(data: dict, key: str) -> str | None:
    setting = SETTINGS[key]
    value = data.get(key)
    if isinstance(value, str):
        try:
            setting.validate(key, value)
        except ValueError:
            pass
        else:
            return value
    return setting.default


def configure(arguments: Sequence[str]) -> str:
    """Inspect or edit saved global defaults without starting a model or session."""
    args = list(arguments) or ["list"]
    action = args[0]
    if action == "path" and len(args) == 1:
        return str(preferences_path())
    if action == "list" and len(args) == 1:
        data = read_preferences()
        return json.dumps({key: _effective(data, key) for key in SETTINGS}, indent=2)
    if action == "get" and len(args) == 2:
        key = args[1]
        _setting(key)
        value = _effective(read_preferences(), key)
        return "null" if value is None else value
    if action == "set" and len(args) == 3:
        key, value = args[1:]
        _setting(key).validate(key, value)
        update_preferences({key: value})
        return f"Saved global default: {key} = {value}. Applies on next launch."
    if action == "unset" and len(args) == 2:
        key = args[1]
        setting = _setting(key)
        update_preferences({}, remove=(key,))
        value = setting.default if setting.default is not None else "null (offline preview)"
        return f"Reset global default: {key} = {value}. Applies on next launch."
    raise ValueError(f"Usage: {USAGE}")


def config_arguments() -> tuple[str, ...]:
    """Complete subcommands, keys, and enum values with the existing completer."""
    return (
        "list",
        "path",
        "get",
        "set",
        "unset",
        *(f"get {key}" for key in SETTINGS),
        *(f"unset {key}" for key in SETTINGS),
        *(f"set {key}" for key in SETTINGS),
        *(f"set {key} {value}" for key, setting in SETTINGS.items() for value in setting.choices),
    )
