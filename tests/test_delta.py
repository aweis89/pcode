"""Diffs drawn by delta, and the Rich fallback whenever delta cannot draw them."""

import shutil
import sys

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.text import Text

from pcode import delta as delta_module
from pcode.delta import ERASE_LINE, SIDE_BY_SIDE_WIDTH, Delta, from_preferences
from pcode.edit_transcript import EditTranscript, prefetch_edits
from pcode.edit_ui import EditBrowser
from pcode.runtime import EditCompleted
from pcode.ui import Transcript

PATCH = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n def f():\n-    return 1\n+    return 2"


def change(patch=PATCH):
    return EditCompleted("one", "x.py", "edited", patch, added=1, removed=1)


class FakeDelta(Delta):
    """Records each render and answers with fixed lines, or fails with None."""

    def render(self, patch, width):
        self.calls.append((patch, width))
        if self.args == ("fail",):
            return None
        return [Text("DELTA " + str(width), style="on red"), Text(""), Text("second row")]


def fake(*args):
    instance = FakeDelta("delta", args)
    object.__setattr__(instance, "calls", [])
    return instance


def test_command_adds_only_what_the_user_did_not_choose():
    delta = Delta("delta")
    assert delta.command(100) == [
        "delta",
        "--paging=never",
        "--width=100",
        "--dark",
        "--file-style=omit",
    ]
    assert "--light" in Delta("delta", light=True).command(100)
    # delta rejects a flag given twice, so the user's own replaces pcode's.
    custom = Delta("delta", ("--width=variable", "--light", "--file-style", "blue", "-s"))
    assert custom.command(SIDE_BY_SIDE_WIDTH) == [
        "delta",
        "--paging=never",
        "--width=variable",
        "--light",
        "--file-style",
        "blue",
        "-s",
    ]


@pytest.mark.parametrize(
    ("layout", "width", "side_by_side"),
    [
        ("auto", SIDE_BY_SIDE_WIDTH - 1, False),
        ("auto", SIDE_BY_SIDE_WIDTH, True),
        ("unified", 400, False),
        ("side-by-side", 40, True),
    ],
)
def test_layout_follows_the_width_only_in_auto(layout, width, side_by_side):
    command = Delta("delta", layout=layout).command(width)
    assert ("--side-by-side" in command) is side_by_side


def test_preferences_choose_delta_only_when_installed(monkeypatch):
    assert from_preferences({}) is None  # conftest hides any installed delta
    monkeypatch.setattr(delta_module, "find_delta", lambda: "/bin/delta")
    assert from_preferences({}) == Delta("/bin/delta")
    assert from_preferences({"diff_renderer": "rich"}) is None
    chosen = from_preferences(
        {"delta_args": "--features 'pcode extra'", "diff_layout": "unified"}, light=True
    )
    assert chosen == Delta("/bin/delta", ("--features", "pcode extra"), "unified", True)
    assert from_preferences({"delta_args": "'unterminated"}).args == ()


def test_erase_to_end_of_line_becomes_padding_in_its_background():
    line = delta_module._line(f"\x1b[41mgone\x1b[0m\x1b[42m{ERASE_LINE}\x1b[0m", 10)
    assert line.plain == "gone      "
    console = Console(width=10, color_system="truecolor", force_terminal=True)
    tail = list(console.render(line[4:]))[0]
    assert tail.style.bgcolor.name == "color(2)"


def test_failing_delta_renders_nothing(tmp_path):
    script = tmp_path / "delta"
    script.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(2)\n")
    script.chmod(0o755)
    assert Delta(str(script)).render(PATCH, 80) is None
    assert Delta(str(tmp_path / "missing")).render(PATCH, 80) is None


def echoing_delta(tmp_path):
    """A stand-in delta that prints its input back and counts its runs."""
    script = tmp_path / "delta"
    runs = tmp_path / "runs"
    script.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"open({str(runs)!r}, 'a').write('run\\n')\n"
        "sys.stdout.write(sys.stdin.read())\n"
    )
    script.chmod(0o755)
    return Delta(str(script)), runs


def test_prefetch_renders_many_patches_in_one_run(tmp_path):
    delta, runs = echoing_delta(tmp_path)
    patches = [PATCH.replace("2", str(n)) for n in range(3, 6)]
    delta.prefetch([*patches, patches[0]], 60)
    assert runs.read_text().count("run") == 1
    for patch in patches:
        assert [line.plain.rstrip() for line in delta.render(patch, 60)] == patch.splitlines()
    assert runs.read_text().count("run") == 1
    # Another width is another layout: rendered again.
    delta.render(patches[0], 61)
    assert runs.read_text().count("run") == 2


def test_failed_batch_falls_back_without_running_each_patch(tmp_path):
    delta, runs = echoing_delta(tmp_path)
    script = tmp_path / "delta"
    script.write_text(script.read_text() + "sys.exit(1)\n")
    patches = [PATCH.replace("2", str(n)) for n in range(3, 6)]
    delta.prefetch(patches, 60)
    assert [delta.render(patch, 60) for patch in patches] == [None] * 3
    assert runs.read_text().count("run") == 1


def test_a_user_width_is_not_padded_to_pcodes():
    erased = f"\x1b[41mx{ERASE_LINE}\x1b[0m\n"
    delta_module._remember((tuple(Delta("d").command(30)), "p"), erased)
    assert Delta("d").render("p", 30)[0].plain == "x" + " " * 29
    custom = Delta("d", ("--width=40",))
    delta_module._remember((tuple(custom.command(30)), "p"), erased)
    assert custom.render("p", 30)[0].plain == "x"


def test_browser_renders_a_whole_view_in_one_run(tmp_path):
    delta, runs = echoing_delta(tmp_path)
    changes = [
        EditCompleted(str(n), f"f{n}.py", "edited", PATCH + f"\n+row {n}", 2, 1) for n in range(3)
    ]
    with create_pipe_input() as pipe:
        ui = EditBrowser(changes, delta=delta, input=pipe, output=SizedOutput())
        for row in range(3):
            ui.files.buffer.cursor_position = ui.files.document.translate_row_col_to_index(row, 0)
            assert f"+row {row}" in ui.diff.text
    assert runs.read_text().count("run") == 1


def test_scrollback_writes_prefetch_their_edit_blocks(tmp_path):
    delta, runs = echoing_delta(tmp_path)
    blocks = [EditTranscript(change(PATCH + f"\n+line {n}"), delta=delta) for n in range(4)]
    prefetch_edits([Text("prose"), *blocks], 70)
    console = Console(width=70, record=True)
    for block in blocks:
        console.print(block)
    assert "+line 3" in console.export_text()
    assert runs.read_text().count("run") == 1


def test_edit_block_uses_delta_lines_and_falls_back_to_rich():
    delta = fake()
    console = Console(width=50, record=True)
    console.print(EditTranscript(change(), delta=delta))
    text = console.export_text()
    assert "DELTA 50" in text and "second row" in text and "-    return 1" not in text
    assert delta.calls == [(PATCH, 50)]

    console = Console(width=50, record=True)
    console.print(EditTranscript(change(), delta=fake("fail")))
    assert "-    return 1" in console.export_text()


def test_transcript_tells_delta_the_palette(monkeypatch):
    monkeypatch.setattr(delta_module, "find_delta", lambda: "/bin/delta")
    transcript = Transcript(Console(), "light", preferences={}, detected_theme="dark")
    assert transcript.delta.light
    transcript.theme = "dark"
    assert not transcript.delta.light
    assert Transcript(Console(), preferences={"diff_renderer": "rich"}).delta is None


class SizedOutput(DummyOutput):
    columns = 83

    def get_size(self):
        return Size(rows=40, columns=self.columns)


def test_browser_shows_delta_rows_styled_and_searchable():
    delta = fake()
    output = SizedOutput()
    with create_pipe_input() as pipe:
        ui = EditBrowser([change()], delta=delta, input=pipe, output=output)
        rows = ui.diff.text.splitlines()
        assert rows[0].startswith("Edited x.py") and rows[2:] == ["DELTA 80", "", "second row"]
        assert delta.calls == [(PATCH, 80)]
        styled = ui.lexer.lex_document(Document(ui.diff.text))(2)
        assert "".join(text for _, text in styled) == "DELTA 80"
        assert any("bg:" in style for style, _ in styled)
        # The heading is still classified as a plain diff line.
        assert ui.lexer.lex_document(Document(ui.diff.text))(0)[0][1] == rows[0]

        output.columns = 103
        ui.rewidth(ui.app)
        assert ui.diff.text.splitlines()[2] == "DELTA 100"


def test_browser_falls_back_to_the_plain_patch():
    with create_pipe_input() as pipe:
        ui = EditBrowser([change()], delta=fake("fail"), input=pipe, output=DummyOutput())
        assert "-    return 1" in ui.diff.text and ui.lexer.rows == {}


@pytest.mark.skipif(shutil.which("delta") is None, reason="delta is optional")
def test_real_delta_renders_the_change():
    delta = Delta(shutil.which("delta"), ("--no-gitconfig",))
    unified = delta.render(PATCH, 80)
    assert unified and any("return 2" in line.plain for line in unified)
    assert not unified[0].plain.strip() == ""
    assert all("\x1b" not in line.plain for line in unified)
    wide = delta.render(PATCH, SIDE_BY_SIDE_WIDTH)
    assert any("return 1" in line.plain and "return 2" in line.plain for line in wide)
    # One batched run gives each patch what its own run gives it.
    patches = [PATCH, PATCH.replace("x.py", "y.md"), PATCH.replace("2", "3")]
    delta_module._cache.clear()
    alone = [delta.render(patch, 81) for patch in patches]
    delta_module._cache.clear()
    delta.prefetch(patches, 81)
    assert len(delta_module._cache) == len(patches)
    assert [delta.render(patch, 81) for patch in patches] == alone
