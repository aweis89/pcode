"""The icon reference stays searchable and honest about terminal widths."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "icons.py"
_spec = importlib.util.spec_from_file_location("icons", SCRIPT)
icons = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(icons)


def test_palette_has_named_printable_glyphs():
    rows = list(icons.matching_icons("all", "", False))
    assert {row[0] for row in rows} == set(icons.ICONS)
    assert len(rows) > 100
    assert all(glyph.isprintable() and name and width in (1, 2) for _, glyph, name, width in rows)


@pytest.mark.parametrize("search", ["∴", "therefore", "THEREFORE", "U+2234"])
def test_search_finds_glyph_name_or_code(search):
    assert list(icons.matching_icons("all", search, False)) == [("thinking", "∴", "THEREFORE", 1)]


def test_category_and_single_cell_filters():
    rows = list(icons.matching_icons("thinking", "", True))
    assert rows and all(group == "thinking" and width == 1 for group, _, _, width in rows)
    assert not any(glyph == "🧠" for _, glyph, _, _ in rows)
    assert list(icons.matching_icons("arrows", "therefore", False)) == []


@pytest.mark.parametrize("width", [40, 100])
@pytest.mark.parametrize(
    "name,glyph,code", [("therefore", "∴", "U+2234"), ("brain", "🧠", "U+1F9E0")]
)
def test_cli_renders_without_a_tty(width, name, glyph, code):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--category", "thinking", "--search", name],
        env={**os.environ, "COLUMNS": str(width), "NO_COLOR": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert glyph in result.stdout
    assert code in result.stdout
    assert name in result.stdout
    assert "Thinking" in result.stdout
    assert "\x1b[" not in result.stdout


def test_empty_search_reports_no_matches(capsys):
    assert icons.main(["--search", "not-an-icon-name"]) == 1
    assert "No matching icons." in capsys.readouterr().out
