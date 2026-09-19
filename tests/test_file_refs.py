"""`@` file references: listing, ranking, and what gets inserted."""

import asyncio
import subprocess

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pcode.commands import CommandRegistry
from pcode.file_refs import FileReferenceCompleter, WorkspaceFiles, reference_fragment
from pcode.ui import create_prompt


def build_tree(root):
    (root / "src" / "pcode").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "pcode" / "ui.py").write_text("ui")
    (root / "src" / "pcode" / "app.py").write_text("app")
    (root / "tests" / "test_ui.py").write_text("test")
    (root / "README.md").write_text("readme")
    return root


def completions(completer, text):
    return list(completer.get_completions(Document(text), CompleteEvent()))


def test_fragment_requires_a_word_boundary():
    assert reference_fragment("look at @src/ui") == "src/ui"
    assert reference_fragment("@") == ""
    assert reference_fragment("mail me@example.com") is None
    assert reference_fragment("@src/ui ") is None
    assert reference_fragment("") is None


def test_walk_skips_hidden_and_ignored_directories(tmp_path):
    build_tree(tmp_path)
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("x")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("x")
    (tmp_path / ".secret").write_text("x")

    paths = WorkspaceFiles(tmp_path).paths()

    assert set(paths) == {
        "README.md",
        "src/pcode/ui.py",
        "src/pcode/app.py",
        "tests/test_ui.py",
    }


def test_git_listing_respects_gitignore(tmp_path):
    build_tree(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.py\n")
    (tmp_path / "ignored.py").write_text("x")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)

    paths = WorkspaceFiles(tmp_path).paths()

    assert "src/pcode/ui.py" in paths
    assert "ignored.py" not in paths


def test_basename_matches_outrank_directory_matches(tmp_path):
    build_tree(tmp_path)
    files = WorkspaceFiles(tmp_path)

    assert files.matches("ui.py")[0] == "src/pcode/ui.py"
    assert files.matches("pcode/a") == ["src/pcode/app.py"]
    assert files.matches("")[0] == "README.md"


def test_listing_is_cached_until_it_expires(tmp_path):
    build_tree(tmp_path)
    files = WorkspaceFiles(tmp_path, cache_seconds=1000)
    assert "later.py" not in files.paths()

    (tmp_path / "later.py").write_text("x")
    assert "later.py" not in files.paths()

    files.cache_seconds = 0
    assert "later.py" in files.paths()


def test_completion_replaces_the_trigger_with_a_relative_path(tmp_path):
    build_tree(tmp_path)
    completer = FileReferenceCompleter(tmp_path)

    [completion] = completions(completer, "explain @app.py")

    assert completion.text == "./src/pcode/app.py "
    assert completion.start_position == -len("@app.py")
    assert completion.display_text == "src/pcode/app.py"
    document = Document("explain @app.py")
    assert (
        document.text[: len(document.text) + completion.start_position] + completion.text
        == "explain ./src/pcode/app.py "
    )


def test_no_completions_without_a_trigger(tmp_path):
    build_tree(tmp_path)
    completer = FileReferenceCompleter(tmp_path)

    assert completions(completer, "explain app.py") == []
    assert completions(completer, "explain @nosuchfile") == []


def test_prompt_inserts_the_reference_from_the_menu(tmp_path):
    build_tree(tmp_path)

    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                CommandRegistry(), workspace=tmp_path, input=pipe, output=DummyOutput()
            )

            async def feed():
                # Completion is asynchronous: wait for the menu the way a typist
                # would, rather than batching every keystroke at once.
                pipe.send_text("explain @app.py")
                while not prompt.default_buffer.text.endswith("@app.py"):
                    await asyncio.sleep(0.01)
                pipe.send_text("\t")
                while not (
                    prompt.default_buffer.complete_state
                    and prompt.default_buffer.complete_state.current_completion
                ):
                    await asyncio.sleep(0.01)
                # The first Enter accepts the selection; the second one sends.
                pipe.send_text("\r\r")

            task = asyncio.ensure_future(feed())
            try:
                return await asyncio.wait_for(prompt.prompt_async(), timeout=5)
            finally:
                task.cancel()

    assert asyncio.run(run()) == "explain ./src/pcode/app.py "
