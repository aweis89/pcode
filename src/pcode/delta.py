"""Diffs rendered by delta (https://dandavison.github.io/delta/) when it is installed.

delta reads the user's git config, so a `[delta]` section or a named feature
(`delta_args = --features pcode`) customizes it here as it does in git. pcode
adds only what it must decide itself (width, dark or light, no pager, and the
layout) and leaves out any of those the user's own arguments already pass:
delta rejects a flag given twice. Anything that goes wrong (delta missing,
unknown arguments, a timeout) returns None, and the caller falls back to its
own Rich rendering.
"""

import os
import secrets
import shlex
import shutil
import subprocess
from collections import OrderedDict
from dataclasses import dataclass

from rich.text import Text

LAYOUTS = ("auto", "unified", "side-by-side")
WIDTH_FLAGS = {"-w", "--width"}
# `auto` switches to side-by-side at this width: each half then keeps about 90
# columns, enough for most code lines beside their line numbers.
SIDE_BY_SIDE_WIDTH = 180
TIMEOUT = 2.0
# A batch gets a little longer per patch, up to this.
MAX_TIMEOUT = 5.0
# delta extends a background to the edge with "erase to end of line", which
# neither Rich nor prompt_toolkit keeps; it is replaced with padding instead.
ERASE_LINE = "\x1b[0K"
_MARK = "\ue000"


@dataclass(frozen=True)
class Delta:
    executable: str
    args: tuple[str, ...] = ()
    layout: str = "auto"
    light: bool = False

    def side_by_side(self, width: int) -> bool:
        if self.layout == "auto":
            return width >= SIDE_BY_SIDE_WIDTH
        return self.layout == "side-by-side"

    def given(self) -> set[str]:
        """The flags the user's own arguments set."""
        return {arg.split("=", 1)[0] for arg in self.args if arg.startswith("-")}

    def command(self, width: int) -> list[str]:
        given = self.given()
        defaults = [
            ({"--paging"}, "--paging=never"),
            (WIDTH_FLAGS, f"--width={width}"),
            ({"--dark", "--light"}, "--light" if self.light else "--dark"),
            # The block heading already names the file.
            ({"--file-style"}, "--file-style=omit"),
        ]
        if self.side_by_side(width):
            defaults.append(({"-s", "--side-by-side"}, "--side-by-side"))
        added = [flag for names, flag in defaults if not names & given]
        return [self.executable, *added, *self.args]

    def render(self, patch: str, width: int) -> list[Text] | None:
        """One styled Rich line per output line, or None if delta failed."""
        command = tuple(self.command(max(1, width)))
        key = (command, patch)
        if key in _cache:
            _cache.move_to_end(key)
            output = _cache[key]
        else:
            output = _remember(key, _run(command, patch))
        if output is None:
            return None
        lines = output.splitlines()
        # With the file section omitted, delta still opens with its blank line.
        while lines and not lines[0].strip():
            lines.pop(0)
        # Padding to pcode's width would wrap every row of a wider user width.
        pad = 0 if self.given() & WIDTH_FLAGS else width
        return [_line(line, pad) for line in lines]

    def prefetch(self, patches: list[str], width: int) -> None:
        """Render many patches through one delta process, ready for `render`.

        Starting delta costs tens of milliseconds, and a scrollback rebuild
        renders every edit block at once. The patches go in one input,
        separated by a marker line delta passes through untouched, since only
        `+`, `-`, space and backslash lines continue a hunk. If the output
        does not split back into one piece per patch, nothing is kept, and
        `render` runs each patch alone. If delta fails outright, every patch is
        recorded as failed, so the blocks fall back to Rich at once instead of
        each waiting on its own failing run.
        """
        command = tuple(self.command(max(1, width)))
        missing = list(dict.fromkeys(p for p in patches if (command, p) not in _cache))
        if len(missing) < 2:
            return
        marker = f"pcode-delta-{secrets.token_hex(8)}"
        output = _run(command, f"\n{marker}\n".join(missing), len(missing))
        if output is None:
            for patch in missing:
                _remember((command, patch), None)
            return
        pieces = output.split(f"{marker}\n")
        if len(pieces) != len(missing):
            return
        for patch, piece in zip(missing, pieces, strict=True):
            _remember((command, patch), piece if piece.strip() else None)


def from_preferences(preferences: dict[str, str], *, light: bool = False) -> Delta | None:
    """The configured delta, or None to render diffs with Rich."""
    if preferences.get("diff_renderer", "delta") != "delta":
        return None
    executable = find_delta()
    if executable is None:
        return None
    try:
        args = tuple(shlex.split(preferences.get("delta_args", "")))
    except ValueError:
        args = ()
    layout = preferences.get("diff_layout", "auto")
    return Delta(executable, args, layout if layout in LAYOUTS else "auto", light)


def find_delta() -> str | None:
    return shutil.which("delta")


# delta's output by (command, patch): a rebuild at the same width and palette
# reuses it rather than starting delta again.
_cache: OrderedDict[tuple[tuple[str, ...], str], str | None] = OrderedDict()
CACHE_SIZE = 256


def _remember(key: tuple[tuple[str, ...], str], output: str | None) -> str | None:
    _cache[key] = output
    _cache.move_to_end(key)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return output


def _run(command: tuple[str, ...], patch: str, count: int = 1) -> str | None:
    try:
        done = subprocess.run(
            command,
            input=patch + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(TIMEOUT + 0.05 * count, MAX_TIMEOUT),
            # A user's --paging=always would start a pager holding the pipes.
            env={**os.environ, "DELTA_PAGER": "cat"},
            # No controlling terminal: delta must never query or write the
            # terminal pcode is drawing on.
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    return done.stdout


def _line(raw: str, width: int) -> Text:
    """Parse one ANSI line, padding where delta erased to the end of the line."""
    if ERASE_LINE not in raw:
        return Text.from_ansi(raw)
    head = raw.split(ERASE_LINE, 1)[0]
    text = Text.from_ansi(head + _MARK)
    style = next(
        (span.style for span in reversed(text.spans) if span.start <= len(text) - 1 < span.end),
        "",
    )
    text.right_crop(1)
    pad = width - text.cell_len
    if pad > 0:
        text.append(" " * pad, style)
    return text
