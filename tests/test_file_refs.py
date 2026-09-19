"""`@` file references: listing, ranking, and what gets inserted."""

import asyncio
import subprocess

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pcode.commands import CommandRegistry
from pcode.file_refs import (
    INLINE_FILE_LIMIT,
    INLINE_HEADER,
    FileReferenceCompleter,
    ReferenceLexer,
    WorkspaceFiles,
    inline_references,
    reference_fragment,
    referenced_paths,
    typed_prompt,
)
from pcode.ui import PALETTES, create_prompt


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


def test_a_path_with_spaces_is_quoted(tmp_path):
    build_tree(tmp_path)
    (tmp_path / "design notes.md").write_text("x")
    completer = FileReferenceCompleter(tmp_path)

    [completion] = completions(completer, "read @design")

    assert completion.text == '"./design notes.md" '


def test_no_completions_without_a_trigger(tmp_path):
    build_tree(tmp_path)
    completer = FileReferenceCompleter(tmp_path)

    assert completions(completer, "explain app.py") == []
    assert completions(completer, "explain @nosuchfile") == []


def test_completion_meta_shows_size_and_whether_it_is_inlined(tmp_path):
    build_tree(tmp_path)
    (tmp_path / "huge.py").write_text("x" * (INLINE_FILE_LIMIT + 1))
    files = WorkspaceFiles(tmp_path)

    assert files.describe("src/pcode/app.py") == "3 B · inlined"
    assert files.describe("huge.py").endswith("· path only")


def test_short_referenced_files_ride_along_with_the_prompt(tmp_path):
    build_tree(tmp_path)

    sent = inline_references("compare ./src/pcode/app.py and ./README.md", tmp_path)

    assert sent.startswith("compare ./src/pcode/app.py and ./README.md\n\n" + INLINE_HEADER)
    assert "=== ./src/pcode/app.py (1 line, 3 B) ===\napp\n=== end ./src/pcode/app.py ===" in sent
    assert "=== ./README.md (1 line, 6 B) ===\nreadme\n=== end ./README.md ===" in sent
    # The typed prompt survives the round trip, for scrollback and for editing.
    assert typed_prompt(sent) == "compare ./src/pcode/app.py and ./README.md"


def test_long_and_unreadable_references_are_named_not_inlined(tmp_path):
    build_tree(tmp_path)
    (tmp_path / "huge.py").write_text("x" * (INLINE_FILE_LIMIT + 1))
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00\xff")

    sent = inline_references("see ./huge.py ./logo.png ./missing.py ./src", tmp_path)
    appended = sent.split(INLINE_HEADER, 1)[1]

    assert "./huge.py (15.6 KB): not inlined" in appended
    # Binary, missing, and directory references stand on their own in the prose.
    assert "./logo.png" not in appended
    assert "./missing.py" not in appended
    assert "./src" not in appended


def test_a_prompt_without_references_is_sent_unchanged(tmp_path):
    build_tree(tmp_path)
    text = "explain the layout, and mail me@example.com about @unfinished"

    assert referenced_paths(text) == []
    assert inline_references(text, tmp_path) == text


def test_quoted_and_relative_references_are_recognized():
    text = 'read "./design notes.md" and ./src/ui.py and ../sibling.py'

    assert referenced_paths(text) == ["./design notes.md", "./src/ui.py", "../sibling.py"]


def test_references_are_styled_in_the_editor():
    lexer = ReferenceLexer()

    fragments = lexer.lex_document(Document("see ./src/ui.py now"))(0)

    assert fragments == [("", "see "), ("class:reference", "./src/ui.py"), ("", " now")]
    for palette in PALETTES.values():
        attrs = palette.prompt_style().get_attrs_for_style_str("class:reference")
        assert attrs.underline


def test_a_live_turn_sends_contents_but_echoes_the_typed_prompt(tmp_path):
    from io import StringIO
    from unittest.mock import MagicMock

    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.runtime import Message
    from pcode.ui import TerminalOutput

    build_tree(tmp_path)
    sent = []

    class Runtime:
        session = None

        async def stream(self, prompt):
            sent.append(prompt)
            yield Message("done")

    async def run():
        buffer = StringIO()
        app = PreviewApp(
            model="test:local",
            runtime=Runtime(),
            workspace=tmp_path,
            console=Console(file=buffer, color_system=None),
        )
        output = TerminalOutput(app.transcript.console, MagicMock())
        output.app.output.get_size.return_value.columns = 80
        app.transcript.output = output
        assert await app.run_live(output, "explain ./src/pcode/app.py")
        await output.flush()
        return buffer.getvalue()

    scrollback = asyncio.run(run())

    assert sent[0].startswith("explain ./src/pcode/app.py\n\n" + INLINE_HEADER)
    assert "=== ./src/pcode/app.py (1 line, 3 B) ===" in sent[0]
    # Scrollback and the task panel echo the prompt, never the payload.
    assert INLINE_HEADER not in scrollback


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
