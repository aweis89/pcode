#!/usr/bin/env python3
"""Browse terminal icon candidates in your actual font.

Run `make icons`, or `uv run python scripts/icons.py --help` for filters.
This is a curated Unicode palette, not an inventory of installed font glyphs.
"""

from __future__ import annotations

import argparse
import unicodedata
from collections.abc import Sequence

from rich.cells import cell_len
from rich.console import Console
from rich.table import Table
from rich.text import Text

ICONS = {
    "status": "✓ ✔ ✗ ✘ ! ? ☐ ☑ ☒ ○ ◉ ● ◐ ◑ ◒ ◓ ⧗ ⌛ ⏳",
    "thinking": "∴ ∵ ⋮ ⋯ … ∷ ≈ ◌ ※ ⁂ ✧ ✦ ✱ ✳ ✶ 💭 🧠",
    "arrows": "← → ↑ ↓ ↔ ↕ ↗ ↘ ↙ ↖ ↩ ↪ ↳ ↴ ⇢ ⇒ ⇥ › » ❯ ▸ ▹ ◂ ◃",
    "bullets": "· • ‣ ⁃ ∙ ◦ ▪ ▫ ■ □ ◆ ◇ ◈ ▲ △ ▼ ▽ ★ ☆",
    "borders": "│ ┃ ┆ ┊ ─ ━ ┄ ┈ ┌ ┐ └ ┘ ├ ┤ ┬ ┴ ┼ ╭ ╮ ╰ ╯ ║ ═ ▏ ▎ ▍ ▌",
    "spinners": "◜ ◠ ◝ ◞ ◡ ◟ ⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏ ⟳ ⟲ ↻ ↺",
    "misc": "⌂ ⌘ ⌥ ⎋ ⏎ ⌫ ␣ § ¶ † ‡ ⚑ ⚐ ⚙ ⚠ ⚡ ⊕ ⊖ ⊗ ⊙ ∅ ∞ ≡",
}


def matching_icons(category: str, search: str, single_cell: bool):
    """Yield category, glyph, Unicode name, and Rich's estimated cell width."""
    needle = search.casefold()
    for group, glyphs in ICONS.items():
        if category != "all" and category != group:
            continue
        for glyph in glyphs.split():
            name = unicodedata.name(glyph)
            code = f"U+{ord(glyph):04X}"
            width = cell_len(glyph)
            if single_cell and width != 1:
                continue
            if needle and needle not in f"{group} {glyph} {name} {code}".casefold():
                continue
            yield group, glyph, name, width


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category", choices=["all", *ICONS], default="all")
    parser.add_argument("--search", default="", help="Match glyph, Unicode name, code, or category")
    parser.add_argument("--single-cell", action="store_true", help="Only estimated one-cell glyphs")
    args = parser.parse_args(argv)
    console = Console()
    console.print("Terminal icon palette", style="bold")
    console.print(
        "Curated candidates, not a font inventory. Missing glyphs, emoji presentation, "
        "and actual widths depend on your terminal/font. Cells are Rich's estimate. "
        "Spinner frames below are static.",
        style="dim",
    )
    console.print(
        "Samples: normal / dim / italic. Rails help reveal spacing and slant.", style="dim"
    )
    matches = list(matching_icons(args.category, args.search, args.single_cell))
    if not matches:
        console.print("No matching icons.")
        return 1
    for group in ICONS:
        rows = [row for row in matches if row[0] == group]
        if not rows:
            continue
        compact = console.width < 60
        if compact:
            console.print()
            console.print(group.title(), style="bold")
        table = Table(title=group.title(), title_justify="left", box=None, padding=(0, 1))
        for heading in ("Normal", "Dim", "Italic", "Cells", "Code", "Unicode name"):
            table.add_column(heading, no_wrap=heading != "Unicode name")
        for _, glyph, name, width in rows:
            samples = []
            for style in ("", "dim", "italic"):
                sample = Text("|")
                sample.append(glyph, style=style)
                sample.append("|")
                samples.append(sample)
            if compact:
                console.print(Text.assemble(samples[0], "  ", samples[1], "  ", samples[2]))
                console.print(f"U+{ord(glyph):04X} · {width} cell(s)")
                console.print(Text(name.lower()))
                console.print()
            else:
                table.add_row(*samples, str(width), f"U+{ord(glyph):04X}", name.lower())
        if not compact:
            console.print()
            console.print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
