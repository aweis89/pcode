"""Diffs drawn by delta, and the Rich fallback whenever delta cannot draw them."""

import asyncio
import shutil
import sys

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.text import Text

from pcode import delta as delta_module
from pcode.delta import ERASE_LINE, SIDE_BY_SIDE_WIDTH, Delta, from_preferences, preview_patch
from pcode.edit_transcript import LiveDeltaPreview
from pcode.edit_ui import render_review
from pcode.git_diff import Review
from pcode.runtime import EditCompleted
from pcode.tool_panel import panel_fragments
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
        "--no-gitconfig",
        "--paging=never",
        "--width=100",
        "--dark",
        "--file-style=omit",
        "--hunk-header-style=omit",
    ]
    assert "--light" in Delta("delta", light=True).command(100)
    # delta rejects a flag given twice, so the user's own replaces pcode's.
    custom = Delta("delta", ("--width=variable", "--light", "--file-style", "blue", "-s"))
    assert custom.command(SIDE_BY_SIDE_WIDTH) == [
        "delta",
        "--no-gitconfig",
        "--paging=never",
        "--hunk-header-style=omit",
        "--width=variable",
        "--light",
        "--file-style",
        "blue",
        "-s",
    ]
    assert Delta("d", ("--no-gitconfig",)).command(80).count("--no-gitconfig") == 1
    headers = Delta("d", ("--hunk-header-style=syntax",)).command(80)
    assert "--hunk-header-style=omit" not in headers


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


def test_the_default_layout_is_unified_at_every_width():
    assert "--side-by-side" not in Delta("delta").command(SIDE_BY_SIDE_WIDTH * 2)


def test_a_side_by_side_flag_in_the_arguments_is_the_layout():
    assert Delta("delta", ("-s",), layout="unified").side_by_side(40)


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
    [line] = delta_module._rows(f"\x1b[41mgone\x1b[0m\x1b[42m{ERASE_LINE}\x1b[0m", 10, False)
    assert line.plain == "gone      "
    console = Console(width=10, color_system="truecolor", force_terminal=True)
    tail = list(console.render(line[4:]))[0]
    assert tail.style.bgcolor.name == "color(2)"


def test_long_rows_fold_under_the_line_number_gutter():
    # Unified delta never wraps; left to the terminal, a row restarts under the numbers.
    raw = f"\x1b[42m  1 ⋮  2 │abcdefghijklmnopqrstuvwxy{ERASE_LINE}\x1b[0m"
    rows = delta_module._rows(raw, 20, True)
    assert [row.plain for row in rows] == [
        "  1 ⋮  2 │abcdefghij",
        "         │klmnopqrst",
        "         │uvwxy     ",
    ]
    console = Console(width=20, color_system="truecolor", force_terminal=True)
    tail = list(console.render(rows[2][-1:]))[0]
    assert tail.style.bgcolor.name == "color(2)"
    # Without line numbers a bar is just code, and wide characters never split.
    assert [row.plain for row in delta_module._rows("a│b界界", 4, False)] == ["a│b", "界界"]
    assert [row.plain for row in delta_module._rows("ab❤️❤️❤️c", 4, False)] == ["ab❤️", "❤️❤️", "c"]
    # A user's own --width is never folded or padded.
    assert [row.plain for row in delta_module._rows(raw, 0, True)] == [
        "  1 ⋮  2 │abcdefghijklmnopqrstuvwxy"
    ]


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


def test_a_review_renders_every_hunk_of_every_view_in_one_run(tmp_path):
    delta, runs = echoing_delta(tmp_path)
    changes = [
        EditCompleted(str(n), f"f{n}.py", "edited", PATCH + f"\n+row {n}", 2, 1) for n in range(3)
    ]
    review = Review("Net", changes, "none", uncommitted=changes[:1], since_review=changes[1:])
    rendered = render_review(review, delta, 60)
    assert runs.read_text().count("run") == 1 and len(rendered) == 3
    # One group per output line (this stand-in echoes the header too).
    for change, groups in zip(changes, rendered.values(), strict=True):
        assert [g[0].plain.rstrip() for g in groups] == change.patch.splitlines()
    # Already rendered at this width: nothing runs again.
    assert render_review(review, delta, 60, rendered) == rendered
    assert runs.read_text().count("run") == 1
    assert render_review(review, None, 60) == {}


def test_transcript_tells_delta_the_palette(monkeypatch):
    monkeypatch.setattr(delta_module, "find_delta", lambda: "/bin/delta")
    transcript = Transcript(Console(), "light", preferences={}, detected_theme="dark")
    assert transcript.delta.light
    transcript.theme = "dark"
    assert not transcript.delta.light
    assert Transcript(Console(), preferences={"diff_renderer": "rich"}).delta is None


class SizedOutput(DummyOutput):
    columns = 84

    def get_size(self):
        return Size(rows=40, columns=self.columns)


def test_render_all_marks_each_failed_patch():
    delta = Delta("/nonexistent/delta")
    assert delta.render_all([PATCH, PATCH + "\n+x"], 80) == [None, None]


@pytest.mark.skipif(shutil.which("delta") is None, reason="delta is optional")
def test_real_delta_renders_the_change(monkeypatch, tmp_path):
    # Neither the git config nor delta's environment reaches pcode's diffs.
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / ".gitconfig").write_text("[delta]\n    side-by-side = true\n")
    monkeypatch.setenv("DELTA_FEATURES", "+side-by-side")
    delta = Delta(shutil.which("delta"), layout="auto")
    unified = delta.render(PATCH, 80)
    assert unified and any("return 2" in line.plain for line in unified)
    assert not unified[0].plain.strip() == ""
    assert all("\x1b" not in line.plain for line in unified)
    # Unified, despite side-by-side in both the git config and DELTA_FEATURES.
    assert not any("return 1" in line.plain and "return 2" in line.plain for line in unified)
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


def test_environment_drops_deltas_own_settings(monkeypatch):
    monkeypatch.setenv("DELTA_FEATURES", "+side-by-side")
    monkeypatch.setenv("BAT_THEME", "Dracula")
    monkeypatch.setenv("DELTA_PAGER", "less")
    env = delta_module._environment()
    assert "DELTA_FEATURES" not in env and "BAT_THEME" not in env
    assert env["DELTA_PAGER"] == "cat" and env["PATH"]


def test_preview_patch_numbers_one_hunk_from_the_preview_lines():
    body = "rtial line\n-old one\n-old two\n+new"
    assert preview_patch("src/x.py", body) == (
        "--- a/src/x.py\n+++ b/src/x.py\n@@ -1,2 +1,1 @@\n-old one\n-old two\n+new"
    )


class PreviewDelta(Delta):
    """Answers with each patch line on a green background, without its gutter."""

    def render(self, patch, width, *, cache=True):
        assert not cache
        if "+boom" in patch:
            return None
        return [Text(line[1:], style="on green") for line in patch.splitlines()[3:]]


def plain_rows(rows):
    return ["".join(text for _, text in row) if isinstance(row, list) else row[1] for row in rows]


def settle(preview):
    async def wait():
        while preview.running:
            await asyncio.sleep(0.01)

    return wait()


def test_live_preview_shows_new_lines_at_once_and_delta_colors_them_after():
    async def run():
        ready = []
        preview = LiveDeltaPreview(lambda: ready.append(True))
        delta = PreviewDelta("delta")
        # Nothing rendered yet: the lines show at once, laid out as delta will.
        first = preview.rows(delta, "c1", "x.py", "+one", 40, "monokai")
        assert plain_rows(first) == ["one"] and isinstance(first[0], tuple)
        await settle(preview)
        assert ready
        rows = preview.rows(delta, "c1", "x.py", "+one", 40, "monokai")
        assert plain_rows(rows) == ["one"] and any("bg:" in style for style, _ in rows[0])
        # A longer body: delta's rows, then the newest line straight away.
        grown = preview.rows(delta, "c1", "x.py", "+one\n-two", 40, "monokai")
        assert grown[0] == rows[0] and plain_rows(grown) == ["one", "two"]
        assert isinstance(grown[1], tuple)
        await settle(preview)
        rows = preview.rows(delta, "c1", "x.py", "+one\n-two", 40, "monokai")
        assert all(isinstance(row, list) for row in rows)

    asyncio.run(run())


def test_live_preview_never_shows_another_calls_rendering():
    async def run():
        preview = LiveDeltaPreview(lambda: None)
        delta = PreviewDelta("delta")
        preview.rows(delta, "c1", "x.py", "+one", 40, "monokai")
        await settle(preview)
        # Call ids repeat across responses; a new path is a new call.
        assert plain_rows(preview.rows(delta, "c1", "y.py", "+two", 40, "monokai")) == ["two"]
        await settle(preview)
        preview.forget()
        rows = preview.rows(delta, "c1", "y.py", "+three", 40, "monokai")
        assert all(isinstance(row, tuple) for row in rows)
        await settle(preview)

    asyncio.run(run())


def test_a_run_for_a_finished_preview_never_lands_in_the_next():
    async def run():
        preview = LiveDeltaPreview(lambda: None)
        delta = PreviewDelta("delta")
        preview.rows(delta, "c1", "x.py", "+old", 40, "monokai")
        preview.forget()  # that preview ended while its run was going
        rows = preview.rows(delta, "c1", "x.py", "+new", 40, "monokai")
        await settle(preview)
        assert preview.done is None or preview.done[0] == "+new"
        await settle(preview)
        assert plain_rows(preview.rows(delta, "c1", "x.py", "+new", 40, "monokai")) == ["new"]
        assert plain_rows(rows) == ["new"]

    asyncio.run(run())


def test_live_preview_waits_for_delta_side_by_side():
    async def run():
        preview = LiveDeltaPreview(lambda: None)
        delta = PreviewDelta("delta", layout="side-by-side")
        assert preview.rows(delta, "c1", "x.py", "+one", 40, "monokai") is None
        await settle(preview)
        assert preview.rows(delta, "c1", "x.py", "+one\n+two", 40, "monokai")[0][0][1] == "one"
        assert len(preview.rows(delta, "c1", "x.py", "+one\n+two", 40, "monokai")) == 1
        await settle(preview)

    asyncio.run(run())


def test_live_preview_failure_lasts_only_for_its_call():
    async def run():
        preview = LiveDeltaPreview(lambda: None)
        delta = PreviewDelta("delta")
        # No complete line yet, the usual first update: nothing to run delta on.
        assert preview.rows(delta, "c1", "x.py", "", 40, "monokai") is None
        assert not preview.running
        preview.rows(delta, "c1", "x.py", "+boom", 40, "monokai")
        await settle(preview)
        assert preview.rows(delta, "c1", "x.py", "+boom\n+more", 40, "monokai") is None
        assert not preview.running
        assert preview.rows(delta, "c2", "x.py", "+fine", 40, "monokai")
        await settle(preview)
        assert not preview.failed

    asyncio.run(run())


def test_live_preview_outside_an_event_loop_never_starts_delta():
    preview = LiveDeltaPreview(lambda: None)
    rows = preview.rows(PreviewDelta("delta"), "c1", "x.py", "+one", 40, "monokai")
    assert plain_rows(rows) == ["one"] and not preview.running


@pytest.mark.skipif(shutil.which("delta") is None, reason="delta is optional")
def test_real_delta_draws_a_streaming_preview_at_the_panel_width():
    async def run():
        preview = LiveDeltaPreview(lambda: None)
        delta = Delta(shutil.which("delta"))
        body = "-x = 1\n+x = 2\n+" + "y" * 70
        preview.rows(delta, "c1", "x.py", body, 50, "monokai")
        await settle(preview)
        rows = preview.rows(delta, "c1", "x.py", body, 50, "monokai")
        assert all(isinstance(row, list) for row in rows)
        texts = plain_rows(rows)
        assert texts[0].rstrip() == "x = 1" and texts[1].rstrip() == "x = 2"
        assert all(len(text) <= 50 for text in texts) and len(texts) == 4

    asyncio.run(run())


def test_panel_passes_styled_rows_through():
    fragments = panel_fragments([("class:a", "plain"), [("bg:red", "x"), ("", "y")]], 20)
    assert fragments == [("class:a", "plain"), ("", "\n"), ("bg:red", "x"), ("", "y")]


def test_a_batch_that_does_not_split_back_is_left_unrendered(tmp_path):
    delta, runs = echoing_delta(tmp_path)
    script = tmp_path / "delta"
    # Swallows the separators, so the output is one piece for three patches.
    script.write_text(script.read_text().replace("sys.stdin.read()", "'one piece\\n'"))
    patches = [PATCH.replace("2", str(n)) for n in range(30, 33)]
    assert delta.render_all(patches, 60) == [None] * 3
    assert runs.read_text().count("run") == 1  # never started once per patch
    assert not any(key[1] in patches for key in delta_module._cache)


def test_batches_stay_within_one_runs_timeout(tmp_path, monkeypatch):
    delta, runs = echoing_delta(tmp_path)
    monkeypatch.setattr(delta_module, "BATCH", 2)
    patches = [PATCH.replace("2", str(n)) for n in range(10, 15)]
    assert all(delta.render_all(patches, 60))
    assert runs.read_text().count("run") == 3


@pytest.mark.skipif(shutil.which("delta") is None, reason="delta is optional")
def test_real_delta_keeps_a_hunk_that_opens_on_a_blank_line():
    patch = "--- a/x.py\n+++ b/x.py\n@@ -1,4 +1,4 @@\n \n a = 1\n-b = 2\n+b = 3"
    delta = Delta(shutil.which("delta"))
    for batch in ([patch], [patch, PATCH]):
        groups = delta.render_all(batch, 80)[0]
        assert len(groups) == 4 and groups[0][0].plain.strip() == ""
