"""Diffs rendered by delta (https://dandavison.github.io/delta/) when it is installed.

delta never reads the user's git config (`--no-gitconfig`, which also ignores
`--config`) or its own `DELTA_*`/`BAT_*` environment: `delta_args` is the one
place to customize it, so pcode's diffs look the same whatever `git diff` is set
up to do. pcode adds only what it must decide itself (width, dark or light, no
pager, and the layout) and leaves out any of those the user's own arguments
already pass: delta rejects a flag given twice. Anything that goes wrong (delta
missing, unknown arguments, a timeout) returns None, and the caller falls back
to its own Rich rendering.
"""

import os
import secrets
import shlex
import shutil
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass

from rich.text import Text

LAYOUTS = ("auto", "unified", "side-by-side")
WIDTH_FLAGS = {"-w", "--width"}
SIDE_BY_SIDE_FLAGS = {"-s", "--side-by-side"}
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
        if self.given() & SIDE_BY_SIDE_FLAGS:
            return True
        if self.layout == "auto":
            return width >= SIDE_BY_SIDE_WIDTH
        return self.layout == "side-by-side"

    def given(self) -> set[str]:
        """The flags the user's own arguments set."""
        return {arg.split("=", 1)[0] for arg in self.args if arg.startswith("-")}

    def command(self, width: int, *, hunk_headers: bool = True) -> list[str]:
        given = self.given()
        defaults = [
            ({"--paging"}, "--paging=never"),
            (WIDTH_FLAGS, f"--width={width}"),
            ({"--dark", "--light"}, "--light" if self.light else "--dark"),
            # The block heading already names the file.
            ({"--file-style"}, "--file-style=omit"),
        ]
        if not hunk_headers:
            defaults.append(({"--hunk-header-style"}, "--hunk-header-style=omit"))
        if self.side_by_side(width):
            defaults.append((SIDE_BY_SIDE_FLAGS, "--side-by-side"))
        added = [flag for names, flag in defaults if not names & given]
        isolated = [] if "--no-gitconfig" in given else ["--no-gitconfig"]
        return [self.executable, *isolated, *added, *self.args]

    def render(
        self, patch: str, width: int, *, hunk_headers: bool = True, cache: bool = True
    ) -> list[Text] | None:
        """One styled Rich line per output line, or None if delta failed.

        Safe to call off the event loop. Without `cache`, the output is not
        kept, so the live preview's many short-lived bodies do not push the
        settled blocks' renderings out.
        """
        command = tuple(self.command(max(1, width), hunk_headers=hunk_headers))
        key = (command, patch)
        with _lock:
            cached = key in _cache
            if cached:
                _cache.move_to_end(key)
                output = _cache[key]
        if not cached:
            output = _run(command, patch)
            if cache:
                _remember(key, output)
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
        with _lock:
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


# The live edit preview renders from a worker thread.
_lock = threading.Lock()


def _remember(key: tuple[tuple[str, ...], str], output: str | None) -> str | None:
    with _lock:
        _cache[key] = output
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return output


def preview_patch(path: str, body: str) -> str:
    """A unified diff of a streaming edit preview's `-`/`+` lines.

    The preview has no line numbers, so the hunk is numbered from 1 and its
    header is left out of the rendering. The path's extension picks the
    syntax highlighting. A line the preview's tail clip cut short of its
    prefix is dropped.
    """
    lines = [line for line in body.split("\n") if line[:1] in ("-", "+")]
    removed = sum(line.startswith("-") for line in lines)
    header = [f"--- a/{path}", f"+++ b/{path}", f"@@ -1,{removed} +1,{len(lines) - removed} @@"]
    return "\n".join(header + lines)


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
            env=_environment(),
            # No controlling terminal: delta must never query or write the
            # terminal pcode is drawing on.
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    return done.stdout


def _environment() -> dict[str, str]:
    # DELTA_FEATURES, BAT_THEME and the like configure delta behind the
    # user's arguments. A pager could hold the pipes open, so it is `cat`.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("DELTA_", "BAT_"))
    }
    return {**env, "DELTA_PAGER": "cat"}


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
