"""One-line samples of every installed Pygments style, plus how to select one."""

from dataclasses import dataclass
from functools import cache

from rich.console import Console, ConsoleOptions, RenderResult
from rich.syntax import Syntax
from rich.text import Text

from pcode.syntax import transparent_theme

# Short enough to survive a narrow terminal, wide enough to show a keyword, a
# function name, a string, a number, and a comment in one row.
SAMPLE = 'def greet(name="world", times=2):  # sample'


@cache
def _sample(style: str) -> Text:
    """The sample line highlighted by one style, with its background dropped.

    Fenced code is rendered on the terminal's own background, so the gallery
    has to preview the same transparent rendering rather than each style's
    designed background: a row that looked right on its own block could still
    be unreadable in the transcript.
    """
    try:
        text = Syntax(SAMPLE, "python", theme=transparent_theme(style)).highlight(SAMPLE)
    except Exception:  # A broken plugin style must not take the gallery with it.
        return Text(SAMPLE)
    text.rstrip()
    return text


@dataclass(frozen=True)
class SyntaxGallery:
    """Every selectable syntax style, marking the one the transcript is using."""

    styles: tuple[str, ...]
    palette: str = "dark"
    dark: str = ""
    light: str = ""
    # `/colors terminal` replaces both saved styles with the ANSI ones. The
    # samples are RGB by definition, so drawing them would break the promise
    # that mode makes; list the names instead and say why.
    terminal_colors: bool = False

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield Text("Syntax styles", style="pcode.accent")
        yield Text(
            f"dark: {self.dark} · light: {self.light}"
            + (" · unused while /colors is terminal" if self.terminal_colors else ""),
            style="pcode.muted",
            no_wrap=True,
            overflow="ellipsis",
        )
        yield from self._names() if self.terminal_colors else self._samples()
        yield Text()
        yield from self._instructions()

    def _names(self) -> RenderResult:
        yield Text("  " + ", ".join(self.styles), style="pcode.muted")
        yield Text("  Run /colors palette to see each style rendered.", style="pcode.muted")

    def _samples(self) -> RenderResult:
        current = self.dark if self.palette == "dark" else self.light
        width = max(len(name) for name in self.styles) + 2
        for name in self.styles:
            active = name == current
            row = Text(
                "▸ " if active else "  ",
                style="pcode.accent",
                no_wrap=True,
                overflow="ellipsis",
            )
            row.append(name.ljust(width), style="pcode.accent" if active else "pcode.muted")
            row.append_text(_sample(name))
            yield row

    def _instructions(self) -> RenderResult:
        for command, note in (
            ("/syntax NAME", f"style the palette in use ({self.palette})"),
            ("/theme dark|light", "switch palettes, then /syntax for that one"),
            ("/colors terminal", "ignore these styles; use ANSI terminal colors"),
            ("/config set syntax_dark NAME", "save the dark-palette style"),
            ("/config set syntax_light NAME", "save the light-palette style"),
        ):
            line = Text("  ", no_wrap=True, overflow="ellipsis")
            line.append(command.ljust(30), style="pcode.accent")
            line.append(note, style="pcode.muted")
            yield line
        yield Text(
            "  Outside the editor: pcode config set syntax_dark NAME",
            style="pcode.muted",
            no_wrap=True,
            overflow="ellipsis",
        )
