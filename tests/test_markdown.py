from io import StringIO

from rich.console import Console

from pcode.block import RULE
from pcode.markdown import Markdown


def test_horizontal_rule_is_solid_box_drawing_line():
    out = StringIO()
    Console(file=out, width=20, color_system=None).print(Markdown("above\n\n---\n\nbelow"))
    lines = out.getvalue().splitlines()
    assert RULE * 20 in lines
    assert not any(set(line) == {"-"} for line in lines)
