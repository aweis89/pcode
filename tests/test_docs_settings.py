"""The configuration page's settings reference stays in step with `SETTINGS`."""

import re
from pathlib import Path

from pcode.preferences import SETTINGS

PAGE = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"


def test_every_setting_is_documented():
    documented = set(re.findall(r"^\| `([a-z_0-9]+)` \|", PAGE.read_text(), re.MULTILINE))
    assert set(SETTINGS) - documented == set(), "add these to docs/configuration.md"
    assert documented - set(SETTINGS) == set(), "these are no longer settings"
