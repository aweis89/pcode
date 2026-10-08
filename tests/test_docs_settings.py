"""The configuration page's settings reference stays in step with `SETTINGS`."""

import re
from pathlib import Path

from pcode.preferences import SETTINGS

PAGE = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"
ROW = re.compile(r"^\| `([a-z_0-9]+)` \| ([^|]*) \|", re.MULTILINE)
# Read at import: conftest's autouse fixtures swap some defaults (group_tools)
# for the duration of each test.
DEFAULTS = {key: setting.default for key, setting in SETTINGS.items()}


def documented() -> dict[str, str]:
    """Key -> its "Built-in default" cell."""
    return {key: default.strip() for key, default in ROW.findall(PAGE.read_text())}


def test_every_setting_is_documented():
    rows = documented()
    assert set(DEFAULTS) - set(rows) == set(), "add these to docs/configuration.md"
    assert set(rows) - set(DEFAULTS) == set(), "these are no longer settings"


def test_documented_defaults_match():
    rows = documented()
    wrong = {}
    for key, default in DEFAULTS.items():
        cell = rows[key]
        if default is None:
            ok = cell.startswith(("unset", "`null`"))
        else:
            ok = cell.startswith(f"`{default}`")
        if not ok:
            wrong[key] = (cell, default)
    assert wrong == {}, "fix these defaults in docs/configuration.md"
