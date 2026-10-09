"""Homebrew revisions belong to a release, not to subsequent releases."""

import runpy
from pathlib import Path

import pytest

bump = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/bump_formula.py"))["bump"]


@pytest.mark.parametrize(
    ("version", "expected_revision"), [("0.1.2", "  revision 1\n"), ("0.1.3", "")]
)
def test_bump_preserves_revision_only_for_same_release(version, expected_revision):
    text = (
        "class Pcode < Formula\n"
        '  url "https://github.com/cruxwell/pcode/archive/refs/tags/v0.1.2.tar.gz"\n'
        '  sha256 "old"\n'
        "  revision 1\n"
        '  head "https://github.com/cruxwell/pcode.git", branch: "master"\n'
        "end\n"
    )
    url = (
        f"https://github.com/cruxwell/pcode/releases/download/v{version}/pcode-{version}-src.tar.gz"
    )

    result = bump(text, url, "new")

    assert f'  url "{url}"\n  sha256 "new"\n' in result
    assert ("  revision 1\n" in result) == bool(expected_revision)
    assert '  head "https://github.com/cruxwell/pcode.git", branch: "master"\n' in result

    # Bumping again within the same release keeps the revision.
    revised = result.replace('  sha256 "new"\n', '  sha256 "new"\n  revision 2\n')
    assert "  revision 2\n" in bump(revised, url, "newer")
