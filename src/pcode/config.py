"""Defaults commands, shared by the CLI and interactive terminal.

Editing defaults deliberately does not mutate a running conversation. In-session
shortcuts remain the way to change both the active setting and its saved default.

`config project ...` edits the workspace's `.pcode/preferences.json`, which is
layered over the user file at launch (except for `USER_ONLY` keys).
"""

import json
from collections.abc import Sequence
from pathlib import Path

from pcode.preferences import (
    SETTINGS,
    USER_ONLY,
    Setting,
    preferences_path,
    project_preferences_path,
    read_preferences,
    update_preferences,
)

USAGE = "config [list | path | get KEY | set KEY VALUE | unset KEY | project ...]"
PROJECT_USAGE = "config project [list | path | set KEY VALUE | unset KEY]"


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


def _effective_data() -> dict:
    """Both files read strictly, so a broken one is reported instead of hidden."""
    data = read_preferences()
    project = project_preferences_path()
    if project is not None:
        data.update({k: v for k, v in read_preferences(project).items() if k not in USER_ONLY})
    return data


def _project_path() -> Path:
    path = project_preferences_path()
    if path is None:
        raise ValueError("No workspace is selected, so there is no project config to edit.")
    return path


def _configure_project(args: list[str]) -> str:
    action = args[0] if args else "list"
    if action == "path" and len(args) == 1:
        return str(_project_path())
    if action == "list" and len(args) == 1:
        return json.dumps(read_preferences(_project_path()), indent=2)
    if action == "set" and len(args) == 3:
        key, value = args[1:]
        if key in USER_ONLY:
            raise ValueError(f"{key} is user-only; a repository cannot set it. Use config set.")
        _setting(key).validate(key, value)
        update_preferences({key: value}, path=_project_path())
        return f"Saved project default: {key} = {value}. Applies on next launch."
    if action == "unset" and len(args) == 2:
        key = args[1]
        _setting(key)
        update_preferences({}, remove=(key,), path=_project_path())
        return f"Removed project default: {key}. Applies on next launch."
    raise ValueError(f"Usage: {PROJECT_USAGE}")


def configure(arguments: Sequence[str]) -> str:
    """Inspect or edit saved defaults without starting a model or session.

    `list` and `get` report the effective value, with the project overlay
    applied; `set` and `unset` edit the user file.
    """
    args = list(arguments) or ["list"]
    action = args[0]
    if action == "project":
        return _configure_project(args[1:])
    if action == "path" and len(args) == 1:
        return str(preferences_path())
    if action == "list" and len(args) == 1:
        data = _effective_data()
        return json.dumps({key: _effective(data, key) for key in SETTINGS}, indent=2)
    if action == "get" and len(args) == 2:
        key = args[1]
        _setting(key)
        value = _effective(_effective_data(), key)
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
    project_keys = [key for key in SETTINGS if key not in USER_ONLY]
    return (
        "list",
        "path",
        "get",
        "set",
        "unset",
        "project",
        *(f"get {key}" for key in SETTINGS),
        *(f"unset {key}" for key in SETTINGS),
        *(f"set {key}" for key in SETTINGS),
        *(f"set {key} {value}" for key, setting in SETTINGS.items() for value in setting.choices),
        "project list",
        "project path",
        *(f"project set {key}" for key in project_keys),
        *(f"project unset {key}" for key in project_keys),
        *(f"project set {key} {value}" for key in project_keys for value in SETTINGS[key].choices),
    )
