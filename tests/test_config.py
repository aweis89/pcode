import json
import sys
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from rich.console import Console

from pcode.app import PreviewApp, main
from pcode.commands import SlashCompleter
from pcode.config import SETTINGS, configure
from pcode.preferences import (
    load_preferences,
    preferences_path,
    read_preferences,
    save_preferences,
    update_preferences,
)


def test_defaults_and_path_do_not_create_files():
    assert configure(["path"]) == str(preferences_path())
    assert json.loads(configure([])) == {
        "meridian_managed": "off",
        "show_thinking": "off",
        "show_tasks": "on",
        "error_scrollback_lines": "20",
        "regenerate_on_resize": "on",
        "command_scrollback": "off",
        "command_scrollback_lines": "20",
        "theme": "dark",
        "editing_mode": "emacs",
        "autocompact": "off",
        "effort": "default",
        "model": None,
    }
    assert configure(["get", "model"]) == "null"
    assert not preferences_path().parent.exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("meridian_managed", "on"),
        ("meridian_managed", "off"),
        ("theme", "light"),
        ("editing_mode", "vi"),
        ("editing_mode", "emacs"),
        ("autocompact", "on"),
        ("effort", "high"),
        ("model", "test:local"),
    ],
)
def test_set_get_unset(key, value):
    save_preferences(theme="dark")
    assert "next launch" in configure(["set", key, value])
    assert configure(["get", key]) == value
    assert load_preferences()[key] == value
    assert "Reset global default" in configure(["unset", key])
    assert key not in read_preferences()
    assert json.loads(configure(["list"]))[key] == SETTINGS[key].default
    if key != "theme":
        assert load_preferences()["theme"] == "dark"


@pytest.mark.parametrize(
    "args",
    [
        ["set", "meridian_managed", "true"],
        ["set", "unknown", "value"],
        ["get", "unknown"],
        ["unset", "unknown"],
        ["set", "theme", "blue"],
        ["set", "editing_mode", "vim"],
        ["set", "autocompact", "true"],
        ["set", "effort", "max"],
        ["set", "model", ""],
        ["set", "model", "a b"],
        ["set", "theme"],
        ["list", "extra"],
        ["path", "extra"],
        ["unset"],
        ["bad"],
    ],
)
def test_invalid_commands_do_not_write(args):
    with pytest.raises(ValueError):
        configure(args)
    assert not preferences_path().exists()


@pytest.mark.parametrize("content", ["{", "[]", "null"])
def test_broken_file_is_reported_and_never_overwritten(content):
    path = preferences_path()
    path.parent.mkdir(parents=True)
    path.write_text(content)
    for args in (["list"], ["get", "theme"], ["set", "theme", "light"], ["unset", "theme"]):
        with pytest.raises(ValueError):
            configure(args)
        assert path.read_text() == content
    with pytest.raises(ValueError):
        save_preferences(theme="light")
    assert path.read_text() == content
    # Discovery and normal startup still work with malformed JSON.
    assert configure(["path"]) == str(path)
    assert load_preferences() == {}


def test_unknown_keys_survive_config_and_shortcut_writes():
    path = preferences_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"future": {"nested": True}, "theme": "light"}))
    configure(["set", "effort", "high"])
    configure(["unset", "theme"])
    save_preferences(model="test:local")
    assert read_preferences() == {
        "future": {"nested": True},
        "effort": "high",
        "model": "test:local",
    }
    assert "future" not in json.loads(configure([]))


def test_invalid_saved_values_report_builtin_defaults():
    path = preferences_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"theme": [], "effort": 42, "autocompact": True, "model": ""}))
    assert json.loads(configure([])) == {key: setting.default for key, setting in SETTINGS.items()}


def test_failed_atomic_write_keeps_old_file_and_cleans_temporary_file():
    save_preferences(theme="dark")
    with patch("pcode.preferences.os.replace", side_effect=PermissionError("denied")):
        with pytest.raises(PermissionError):
            configure(["set", "theme", "light"])
    assert load_preferences()["theme"] == "dark"
    assert {path.name for path in preferences_path().parent.iterdir()} == {
        "preferences.json",
        "preferences.json.lock",
    }


def test_concurrent_updates_do_not_lose_keys():
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda i: update_preferences({f"key{i}": str(i)}), range(20)))
    assert read_preferences() == {f"key{i}": str(i) for i in range(20)}


def test_config_cli_works_without_terminal_model_or_session(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    with patch("pcode.app.PreviewApp") as app, patch("pcode.agent.create_agent") as agent:
        for args in (["set", "theme", "light"], ["get", "theme"], []):
            monkeypatch.setattr(sys, "argv", ["pcode", "config", *args])
            main()
        app.assert_not_called()
        agent.assert_not_called()
    output = capsys.readouterr().out
    assert "Saved global default" in output
    assert "\nlight\n" in output
    assert not (tmp_path / "sessions").exists()


@pytest.mark.parametrize("failure", ["invalid", "io"])
def test_config_cli_errors_exit_nonzero(monkeypatch, capsys, failure):
    monkeypatch.setattr(sys, "argv", ["pcode", "config", "set", "theme", "invalid"])
    if failure == "io":
        monkeypatch.setattr(
            "pcode.app.configure", lambda _: (_ for _ in ()).throw(PermissionError("denied"))
        )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_slash_config_edits_defaults_not_active_session():
    output = StringIO()
    app = PreviewApp(theme="dark", console=Console(file=output, width=160))
    app.handle("/config set theme light")
    assert app.transcript.theme == "dark"
    assert PreviewApp(console=Console(file=StringIO())).transcript.theme == "light"
    app.handle("/config get theme")
    app.handle("/config path")
    app.handle("/config set autocompact on")  # Works even in offline preview.
    assert load_preferences()["autocompact"] == "on"
    app.handle("/config set theme invalid")
    app.handle('/config set model "unterminated')
    assert "next launch" in output.getvalue()
    assert "theme must be one of" in output.getvalue()
    assert "No closing quotation" in output.getvalue()
    assert load_preferences()["theme"] == "light"


def test_slash_config_io_errors_do_not_crash():
    output = StringIO()
    app = PreviewApp(console=Console(file=output))
    with patch("pcode.app.configure", side_effect=PermissionError("denied")):
        app.handle("/config")
    assert "Could not access global defaults" in output.getvalue()


def test_shortcut_corrupt_config_keeps_active_selection():
    path = preferences_path()
    path.parent.mkdir(parents=True)
    path.write_text("{")
    output = StringIO()
    app = PreviewApp(console=Console(file=output))
    app.theme("light")
    assert app.transcript.theme == "light"
    assert "Could not save defaults" in output.getvalue()
    assert path.read_text() == "{"


@pytest.mark.parametrize(
    "prefix,expected",
    [
        ("/conf", "/config"),
        ("/config get th", "get theme"),
        ("/config set theme l", "set theme light"),
        ("/config set autocompact o", "set autocompact on"),
        ("/config unset ef", "unset effort"),
    ],
)
def test_config_completion(prefix, expected):
    app = PreviewApp(console=Console(file=StringIO()))
    completions = SlashCompleter(app.registry).get_completions(Document(prefix), CompleteEvent())
    assert expected in [completion.text for completion in completions]


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "many", "", " 20", "２０"])
def test_error_scrollback_lines_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="positive integer"):
        configure(["set", "error_scrollback_lines", value])
    assert configure(["get", "error_scrollback_lines"]) == "20"


def test_error_scrollback_settings_round_trip():
    configure(["set", "error_scrollback_lines", "35"])
    with pytest.raises(ValueError, match="Unknown"):
        configure(["set", "error_scrollback", "off"])
    assert load_preferences()["error_scrollback_lines"] == "35"
    assert "error_scrollback" not in load_preferences()
    configure(["unset", "error_scrollback_lines"])
    assert configure(["get", "error_scrollback_lines"]) == "20"
