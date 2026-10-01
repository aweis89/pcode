import asyncio
from pathlib import Path

from prompt_toolkit.document import Document

from pcode.commands import CommandRegistry, SlashCompleter
from pcode.ext import ExtensionAPI
from pcode.path_completion import complete_paths, path_fragment
from pcode.remote import RemoteController, _proxy


def texts(argument, workspace):
    return [completion.text for completion in complete_paths(argument, workspace)]


def test_path_fragment_skips_leading_options():
    assert path_fragment("~/a b") == "~/a b"
    assert path_fragment("--global ~/x") == "~/x"
    assert path_fragment("--global") == ""
    assert path_fragment("--global ") == ""


def test_completes_directories_first_and_hides_dotfiles(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "setup.py").write_text("")
    (tmp_path / ".secret").mkdir()
    (tmp_path / "other").mkdir()
    assert texts("s", tmp_path) == ["src/", "setup.py"]
    assert texts("--global S", tmp_path) == ["src/", "setup.py"]
    assert texts(".", tmp_path) == [".secret/"]
    assert texts(f"{tmp_path}/o", Path("/nowhere")) == ["other/"]
    assert texts("./src/", tmp_path) == []
    assert texts("missing/x", tmp_path) == []


def test_only_the_typed_name_is_replaced(tmp_path):
    (tmp_path / "project").mkdir()
    (completion,) = complete_paths("--global ./pro", tmp_path)
    assert completion.text == "project/" and completion.start_position == -3


def test_home_and_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "work").mkdir()
    assert texts("~", tmp_path) == ["~/"]
    assert texts("~/w", Path("/nowhere")) == ["work/"]
    assert texts("", tmp_path) == []
    assert texts("/", tmp_path) == []


def test_extension_commands_complete_paths_in_process_and_attached(tmp_path):
    (tmp_path / "docs").mkdir()
    api = ExtensionAPI("demo", tmp_path, ui=None, session_dir=tmp_path / "sessions")
    api.register_command("/grant", "Grant", lambda argument: None, complete_paths=True)
    (command,) = api.commands
    registry = CommandRegistry()
    registry.register(command)
    completer = SlashCompleter(registry)
    found = completer.get_completions(Document("/grant d"), None)
    assert [completion.text for completion in found] == ["docs/"]

    async def completed_off_the_loop():
        return [c.text async for c in completer.get_completions_async(Document("/grant d"), None)]

    assert asyncio.run(completed_off_the_loop()) == ["docs/"]

    controller = RemoteController(view=None, activity=type("Activity", (), {})())
    controller.workspace = tmp_path
    proxy = _proxy(
        {"name": "/grant", "description": "", "completer": command.argument_completer.__name__},
        controller,
    )
    assert [completion.text for completion in proxy.argument_completer("d")] == ["docs/"]
