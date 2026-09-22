"""The ponytail ruleset ships off, and its level is shared with ponytail's other plugins."""

import json

import pytest

from pcode.ext import BUNDLED_DIR, Extension, ExtensionUI, load_extension
from pcode.extensions import ponytail


@pytest.fixture(autouse=True)
def no_env_mode(monkeypatch):
    monkeypatch.delenv(ponytail.ENV_VAR, raising=False)


def load(workspace, *, on=frozenset(), ui=None):
    return load_extension(
        Extension("ponytail", BUNDLED_DIR / "ponytail.py", "bundled"),
        workspace,
        ui or ExtensionUI(),
        set(),
        set(on),
    )


def instructions(extension):
    return "\n".join(str(c.get_instructions()) for c in extension.capabilities)


def save_mode(mode, **extra):
    ponytail.config_path().parent.mkdir(parents=True, exist_ok=True)
    ponytail.config_path().write_text(json.dumps({"defaultMode": mode, **extra}))


def test_ships_off_and_contributes_nothing_until_turned_on(tmp_path):
    extension = load(tmp_path)
    assert extension.state() == "off by default (/extensions on ponytail)"
    assert extension.capabilities == [] and extension.commands == []


def test_turning_it_on_injects_the_full_ruleset_and_the_command(tmp_path):
    extension = load(tmp_path, on={"ponytail"})
    assert extension.loaded, extension.error
    assert [command.name for command in extension.commands] == ["/ponytail"]
    text = instructions(extension)
    assert "PONYTAIL MODE ACTIVE — level: full" in text
    assert "the ladder enforced" in text
    assert "YAGNI extremist" not in text  # Only the active level's intensity is sent.


def test_saved_and_environment_levels_are_read_like_the_other_plugins(tmp_path, monkeypatch):
    assert ponytail.read_mode() == "full"
    save_mode("ultra")
    assert ponytail.read_mode() == "ultra"
    monkeypatch.setenv(ponytail.ENV_VAR, "LITE")
    assert ponytail.read_mode() == "lite"
    monkeypatch.setenv(ponytail.ENV_VAR, "nonsense")
    assert ponytail.read_mode() == "ultra"
    ponytail.config_path().write_text("{ not json")
    assert ponytail.read_mode() == "full"


def test_off_keeps_the_command_but_sends_no_instructions(tmp_path):
    save_mode("off")
    extension = load(tmp_path, on={"ponytail"})
    assert instructions(extension) == ""
    assert [command.name for command in extension.commands] == ["/ponytail"]


def run(extension, argument):
    (command,) = extension.commands
    command.handler(argument)


def recorder():
    notices, reloads = [], []
    return (
        notices,
        reloads,
        ExtensionUI(
            lambda text, level: notices.append((text, level)),
            lambda: reloads.append(True),
        ),
    )


def test_setting_a_level_saves_it_and_asks_for_a_reload(tmp_path):
    save_mode("full", quietStartup=True)
    notices, reloads, ui = recorder()
    run(load(tmp_path, on={"ponytail"}, ui=ui), "ultra")
    saved = json.loads(ponytail.config_path().read_text())
    assert saved == {"defaultMode": "ultra", "quietStartup": True}  # Shared file, kept intact.
    assert reloads == [True]
    assert notices == [("Ponytail ultra.", "info")]


def test_the_environment_wins_over_a_saved_level_and_no_reload_is_asked(tmp_path, monkeypatch):
    monkeypatch.setenv(ponytail.ENV_VAR, "lite")
    notices, reloads, ui = recorder()
    run(load(tmp_path, on={"ponytail"}, ui=ui), "ultra")
    assert ponytail.saved_mode() == "ultra" and ponytail.read_mode() == "lite"
    assert reloads == []
    assert notices[0][1] == "warning" and ponytail.ENV_VAR in notices[0][0]


def test_status_reports_the_level_and_an_unknown_one_is_refused(tmp_path):
    save_mode("lite")
    notices, _, ui = recorder()
    extension = load(tmp_path, on={"ponytail"}, ui=ui)
    run(extension, "")
    assert notices == [("Ponytail is lite (saved).", "info")]
    with pytest.raises(ValueError):
        run(extension, "medium")
