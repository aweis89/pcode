"""Plain ASCII stand-ins for the symbols pcode draws, for terminals that can't draw them.

Every stand-in takes the single cell its symbol does, so ASCII mode is just
another encoding of the same screen: layout math, row counts and cursor
movement don't change. That is why it is applied as stdout's codec, the one
place every writer meets (prompt_toolkit, Rich, popups, scrollback), rather
than by editing the symbols where each module draws them. Rich also reads the
encoding's name and draws ASCII table borders by itself.

Only symbols are replaced. Letters in any script pass through untouched.
"""

import codecs
import io
import os
import sys
import unicodedata
from collections.abc import Mapping

from rich.cells import cell_len

CODEC = "pcode-ascii"

# Canonical UI icons in standard Unicode and in Nerd Fonts (Codicons 16x16 UI
# set, plus Octicons' skip and cpu). Every Nerd Font codepoint here is 1 cell
# wide and bundled in the Symbols Nerd Font Mono that Ghostty and kitty (0.36+)
# embed.
ICONS: dict[str, str] = {
    "shell": "$",
    "search": "⌕",
    "edit": "✎",
    "read": "⎘",
    "thought": "❝",
    "agent": "»",
    "pending": "○",
    "completed": "✔",
    "cancelled": "–",
    "blocked": "!",
    "ok": "✓",
    "fail": "✗",
    "tool_done": "√",
    "turn_cancelled": "⊘",
}

NERD_ICONS: dict[str, str] = {
    "shell": "\uea85",  # cod-terminal
    "search": "\uea6d",  # cod-search
    "edit": "\uea73",  # cod-edit
    "read": "\uea7b",  # cod-file
    "thought": "\uec1e",  # cod-sparkle
    "agent": "\uf4bc",  # oct-cpu
    "pending": "\ueb0c",  # cod-issues: same box as cod-pass, unlike cod-circle
    "completed": "\ueba4",  # cod-pass
    "cancelled": "\uf4b7",  # oct-skip
    "blocked": "\uea6c",  # cod-warning
    "ok": "\ueab2",  # cod-check
    "fail": "\uea76",  # cod-close
    "tool_done": "\ueab2",  # cod-check
    "turn_cancelled": "\uf4b7",  # oct-skip
}
# Spinner frames for Nerd Font mode, drawn in the same full-cell circle as the
# task icons (`cod-pass`, `cod-issues`), so they line up where braille and
# `◜◝◞◟` sit small and high. The model gets a filling pie
# (md-circle_slice_1..8), a tool call the arc (extra-progress_spinner_1..6).
NERD_SPINNERS: dict[str, list[str]] = {
    "spinner": [chr(0xF0A9E + i) for i in range(8)],
    "tool_spinner": [chr(0xEE06 + i) for i in range(6)],
}
# Private Use Area chars are not `str.isprintable()` in Python; `plain()`
# preserves only this known set so untrusted PUA input is still sanitized.
NERD_ICON_CHARS: frozenset[str] = frozenset(NERD_ICONS.values()).union(*NERD_SPINNERS.values())

# Symbols outside the ranges `_by_name` sorts out.
_SYMBOLS = {
    "✓": "v",
    "✔": "v",
    "√": "v",
    "✗": "x",
    "✘": "x",
    "×": "x",
    "⊘": "/",
    "•": "*",
    "·": "-",
    "…": ".",
    "⋯": ":",
    "⋮": ":",
    "❝": '"',
    "›": ">",
    "❯": ">",
    "»": ">",
    "«": "<",
    "⟳": "~",
    "⌕": "?",
    "✎": "#",
    "⎘": "=",
    "⧖": "%",
    "−": "-",
    "–": "-",
    "—": "-",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    # Nerd Font icon stand-ins, should any pass through ASCII encoding.
    "\uea85": "$",
    "\uea6d": "?",
    "\uea73": "#",
    "\uea7b": "=",
    "\uec1e": '"',
    "\uf4bc": ">",
    "\ueb0c": "o",
    "\ueba4": "v",
    "\uf4b7": "-",
    "\uea6c": "!",
    "\ueab2": "v",
    "\uea76": "x",
}
_SYMBOLS.update(dict.fromkeys(NERD_SPINNERS["spinner"], "o"))
_SYMBOLS.update(dict.fromkeys(NERD_SPINNERS["tool_spinner"], "*"))

# Unicode blocks drawn from by name: box drawing, block elements, geometric
# shapes, arrows and braille (spinners).
_RANGES = ((0x2190, 0x21FF), (0x2500, 0x25FF), (0x2800, 0x28FF))


def _by_name(char: str) -> str | None:
    """A stand-in read off the character's Unicode name, or None to keep it."""
    name = unicodedata.name(char, "")
    words = set(name.replace("-", " ").split())
    if name.startswith("BRAILLE"):
        return " " if char == "\u2800" else "."
    if "DIAGONAL" in words:
        return "X" if "CROSS" in words else "/" if "UPPER RIGHT TO LOWER LEFT" in name else "\\"
    if name.startswith("BOX DRAWINGS"):
        across = words & {"HORIZONTAL", "LEFT", "RIGHT"}
        down = words & {"VERTICAL", "UP", "DOWN"}
        return "+" if across and down else "-" if across else "|"
    if "ARROW" in words or "POINTING" in words:
        if "CLOCKWISE" in words or "ANTICLOCKWISE" in words:
            return "~"
        for word, stand_in in (("RIGHT", ">"), ("LEFT", "<"), ("UP", "^"), ("DOWN", "v")):
            if f"{word}WARDS" in words or word in words:
                # `↵` (downwards with corner leftwards) reads as a return.
                return "<" if "CORNER" in words else stand_in
        return ">"
    if "BLOCK" in words or "SHADE" in words:
        # `▌`, the quote rail, and the other left/right slivers read as a bar.
        return "|" if words & {"LEFT", "RIGHT"} else "#"
    if "CIRCLE" in words or "ARC" in words:
        return "*" if "BLACK" in words else "o"
    return "*"


def _table() -> dict[int, str]:
    table = {ord(char): stand_in for char, stand_in in _SYMBOLS.items()}
    for start, end in _RANGES:
        for point in range(start, end + 1):
            char = chr(point)
            # A wide symbol would need two stand-ins; those stay as drawn.
            if point not in table and unicodedata.name(char, "") and cell_len(char) == 1:
                stand_in = _by_name(char)
                if stand_in is not None:
                    table[point] = stand_in
    return table


TABLE = _table()


def to_ascii(text: str) -> str:
    return text.translate(TABLE)


def _search(name: str) -> codecs.CodecInfo | None:
    """`pcode-ascii-<base>`: the stand-ins, then the stream's own encoding.

    Wrapping the base keeps a legacy locale's letters (`é` in Latin-1, kana
    in EUC-JP) encoded as its terminal expects. The name never starts with
    `utf`, which is what tells Rich to draw ASCII boxes.
    """
    prefix = CODEC.replace("-", "_") + "_"
    name = name.replace("-", "_")
    if not name.startswith(prefix):
        return None
    base = codecs.lookup(name.removeprefix(prefix))

    class Encoder(codecs.IncrementalEncoder):
        def __init__(self, errors: str = "strict"):
            super().__init__(errors)
            self.base = base.incrementalencoder(errors)

        def encode(self, input: str, final: bool = False) -> bytes:
            self.base.errors = self.errors
            return self.base.encode(input.translate(TABLE), final)

        def reset(self) -> None:
            self.base.reset()

    def encode(text: str, errors: str = "strict") -> tuple[bytes, int]:
        return base.encode(text.translate(TABLE), errors)[0], len(text)

    return codecs.CodecInfo(
        name=f"{CODEC}-{base.name}",
        encode=encode,
        decode=base.decode,
        incrementalencoder=Encoder,
        incrementaldecoder=base.incrementaldecoder,
        streamreader=base.streamreader,
        streamwriter=base.streamwriter,
    )


codecs.register(_search)

_active = False
_nerd = False


def active() -> bool:
    """Whether stdout is drawing ASCII stand-ins."""
    return _active


def nerd() -> bool:
    """Whether UI icons are drawn with Nerd Font codepoints."""
    return _nerd


def icon(name: str) -> str:
    """Return the active 1-cell icon for `name` (Nerd Font when enabled, else Unicode)."""
    return NERD_ICONS[name] if (_nerd and not _active) else ICONS[name]


def legacy_locale(env: Mapping[str, str]) -> bool:
    """Whether the locale names a character set other than UTF-8 (`en_US.ISO-8859-1`).

    A bare `C`, `POSIX` or unset locale says nothing about the terminal (a
    container's default), so only an explicit other charset counts.
    """
    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = env.get(key)
        if value:
            charset = value.partition(".")[2].partition("@")[0]
            return bool(charset) and charset.lower().replace("-", "") != "utf8"
    return False


def wanted(setting: str, env: Mapping[str, str] | None = None, wide: bool | None = None) -> bool:
    """Whether the `glyphs` setting (`auto`, `nerd`, `unicode`, `ascii`) asks for ASCII.

    `wide` is the startup probe's answer: the terminal drew a box-drawing
    line two cells wide, which shifts every row that holds one.
    """
    if setting in ("ascii", "unicode", "nerd"):
        return setting == "ascii"
    env = os.environ if env is None else env
    # The Linux console's fonts lack most of these symbols. Other bare `TERM`s
    # (`vt100`, `ansi`) are as often a Unicode terminal misreporting itself.
    return env.get("TERM", "") == "linux" or legacy_locale(env) or bool(wide)


def wanted_nerd(
    setting: str,
    env: Mapping[str, str] | None = None,
    *,
    ascii_on: bool = False,
) -> bool:
    """Whether `glyphs` asks for Nerd Font icons.

    `auto` turns them on only in terminals known to embed Nerd Fonts out of the
    box (Ghostty, and kitty since 0.36), outside tmux so attaching from another
    terminal does not draw missing-glyph boxes. Users with a local Nerd Font in
    another terminal or tmux can set `glyphs nerd`.

    kitty is trusted by `TERM` alone: `KITTY_WINDOW_ID` is inherited by any
    terminal launched from a kitty shell (VS Code via `code .`, Alacritty).
    """
    if ascii_on or setting in ("ascii", "unicode"):
        return False
    if setting == "nerd":
        return True
    env = os.environ if env is None else env
    if env.get("TMUX") or env.get("STY"):
        return False
    if env.get("TERM") == "xterm-ghostty" or env.get("TERM_PROGRAM") == "ghostty":
        return True
    return env.get("TERM") == "xterm-kitty"


def apply_nerd(on: bool) -> bool:
    """Enable or disable Nerd Font UI icons; ASCII mode always wins."""
    global _nerd
    _nerd = bool(on) and not _active
    return _nerd


def apply(on: bool, stream=None) -> bool:
    """Switch the terminal stream (stdout) to ASCII stand-ins; whether it now draws them.

    Only a terminal is switched: a reply piped elsewhere keeps its symbols.
    """
    global _active, _nerd
    stream = sys.stdout if stream is None else stream
    if not on or not isinstance(stream, io.TextIOWrapper) or not stream.isatty():
        return _active
    stream.flush()
    if not stream.encoding.startswith(CODEC):
        # Keep the error handler: UTF-8 mode's `surrogateescape` lets a file
        # name that isn't UTF-8 print rather than raise.
        stream.reconfigure(encoding=f"{CODEC}-{stream.encoding}", errors=stream.errors)
    _active = True
    _nerd = False
    return True
