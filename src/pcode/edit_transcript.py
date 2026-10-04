"""Literal, width-aware edit blocks, independent of assistant Markdown."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.lexers import Lexer
from pygments.token import Generic, Token
from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text

from pcode.block import DONE, block_heading, block_rule
from pcode.delta import Delta, preview_patch
from pcode.edits import edit_text
from pcode.runtime import EditCompleted
from pcode.syntax import transparent_theme
from pcode.tool_display import command_text


@dataclass(frozen=True)
class EditTranscript:
    change: EditCompleted
    code_theme: str = "monokai"
    max_rows: int = 60
    delta: Delta | None = None

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        change = self.change
        yield block_rule(
            block_heading(
                DONE,
                f"{change.operation.capitalize()} {edit_text(change.path)}"
                f" · +{change.added} −{change.removed}",
            )
        )
        if change.patch:
            text = edit_text(change.patch)
            lines = self.delta.render(text, options.max_width) if self.delta else None
            patch = (
                Text("\n").join(lines)
                if lines is not None
                else Syntax(text, "diff", theme=transparent_theme(self.code_theme), word_wrap=True)
            )
            rows = console.render_lines(patch, options, pad=False)
            for row in rows[: self.max_rows]:
                yield from row
                yield Segment.line()
            if len(rows) > self.max_rows or change.truncated:
                yield Text("… additional diff rows omitted", style="pcode.muted")
        if change.omitted:
            yield Text(f"Diff unavailable: {edit_text(change.omitted)}", style="pcode.muted")
        yield block_rule()


def prefetch_edits(objects, width: int) -> None:
    """Render every delta edit block in `objects` through one delta process each."""
    patches: dict[Delta, list[str]] = {}
    for obj in objects:
        if isinstance(obj, EditTranscript) and obj.delta is not None and obj.change.patch:
            patches.setdefault(obj.delta, []).append(edit_text(obj.change.patch))
    for delta, texts in patches.items():
        delta.prefetch(texts, width)


@cache
def _token_style(code_theme: str, token) -> str:
    """Render one syntax token as a prompt_toolkit style string."""
    style = transparent_theme(code_theme).get_style_for_token(token)
    # Convert only a library-generated marker, never file content. This keeps
    # ANSI palette colors native while matching Rich's RGB syntax colors too.
    marker = Style(color=style.color, bold=style.bold).render("x", color_system="truecolor")
    return to_formatted_text(ANSI(marker))[0][0]


@cache
def _preview_style(code_theme: str, added: bool) -> str:
    token = Generic.Inserted if added else Generic.Deleted
    return "class:bottom-toolbar.text " + _token_style(code_theme, token)


def diff_token(line: str):
    """Classify a logical diff line the way Pygments' diff lexer does."""
    if line.startswith(("+", "> ")):
        return Generic.Inserted
    if line.startswith(("-", "< ")):
        return Generic.Deleted
    if line.startswith("@"):
        return Generic.Subheading
    if line.startswith(("diff", "index", "Index:", "=")):
        return Generic.Heading
    if line.startswith("!"):
        return Generic.Strong
    return Token.Text


@cache
def _rich_style(style: Style) -> str:
    """A Rich style as a prompt_toolkit style string, by way of its ANSI codes."""
    if not style:
        return ""
    # A link would open with an OSC 8 sequence ahead of the marker's colors.
    marker = style.clear_meta_and_links().render("x")
    return to_formatted_text(ANSI(marker))[0][0]


_FRAGMENT_CONSOLE = Console(width=10_000, color_system="truecolor", force_terminal=True)


def text_fragments(text: Text, console: Console = _FRAGMENT_CONSOLE) -> list[tuple[str, str]]:
    """One unwrapped Rich line as prompt_toolkit fragments."""
    # Console.render, unlike Text.render, applies the text's own base style.
    options = console.options.update(no_wrap=True, overflow="ignore")
    return [
        (_rich_style(segment.style), segment.text)
        for segment in console.render(text, options)
        if segment.text and segment.text != "\n"
    ]


class DiffLexer(Lexer):
    """Color diffs in prompt_toolkit with the same theme scrollback diffs use.

    `rows` holds fragments already styled elsewhere (delta's output), by row;
    every other row is classified as a plain diff line.
    """

    def __init__(self, code_theme: str = "monokai") -> None:
        self.code_theme = code_theme
        self._rows: dict[int, list[tuple[str, str]]] = {}
        self._version = 0

    @property
    def rows(self) -> dict[int, list[tuple[str, str]]]:
        return self._rows

    @rows.setter
    def rows(self, rows: dict[int, list[tuple[str, str]]]) -> None:
        self._rows = rows
        self._version += 1

    def lex_document(self, document):
        rows = self.rows

        def line(number: int):
            if number in rows:
                return rows[number]
            text = document.lines[number]
            return [(_token_style(self.code_theme, diff_token(text)), text)]

        return line

    def invalidation_hash(self):
        return self._version


class LiveDeltaPreview:
    """delta rows for the streaming edit preview, rendered off the event loop.

    The preview grows every few dozen milliseconds while a call streams, and
    one delta run takes longer than that, so `rows` never waits. It shows the
    newest rendering delta has finished for this call, and any lines that
    arrived since straight away, the way delta lays them out (no +/- gutter)
    but colored only as added or removed, until the next run highlights them.
    Lines therefore appear as fast as without delta; only their syntax colors
    trail. Side by side cannot be approximated a line at a time, so that
    layout waits for delta. One run is in flight at a time and the newest
    body runs next. None means draw with Rich: delta failed for this call, or
    there is nothing to show yet. `on_ready` runs on the event loop when a
    rendering lands.
    """

    def __init__(self, on_ready: Callable[[], None]) -> None:
        self.on_ready = on_ready
        # Bumped by forget(), so a run still going for a finished preview is
        # never taken for the next one's, even with the same call id and path.
        self.generation = 0
        self.forget()
        self.running = False

    def forget(self) -> None:
        """Drop the current call's renderings; its preview has gone."""
        self.generation += 1
        self.call: tuple | None = None
        self.done: tuple[str, list] | None = None
        self.wanted: tuple | None = None
        self.failed = False

    def rows(self, delta: Delta, call_id: str, path: str, body: str, width: int, code_theme: str):
        if not any(line[:1] in ("-", "+") for line in body.split("\n")):
            return None
        call = (delta, call_id, path, width)
        if self.call is None or self.call[:4] != call:
            # Call ids repeat across responses, so a new path is a new call too.
            self.forget()
            self.call = (*call, self.generation)
        call = self.call
        if self.failed:
            return None
        done = self.done
        if done is None or done[0] != body:
            self.wanted = (call, body)
            if not self.running:
                self._start()
        unified = not delta.side_by_side(width)
        if done is None:
            return _pending_rows(body, width, code_theme) if unified else None
        rendered, rows = done
        if rendered != body and unified and body.startswith(rendered + "\n"):
            return rows + _pending_rows(body[len(rendered) + 1 :], width, code_theme)
        return rows

    def _start(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            key = self.wanted
            (delta, _, path, width, _), body = key
            future = loop.run_in_executor(None, _live_rows, delta, path, body, width)
        except RuntimeError:  # outside an application, or the loop is closing
            return
        self.running = True
        future.add_done_callback(lambda future: self._finished(key, future))

    def _finished(self, key: tuple, future) -> None:
        self.running = False
        call, body = key
        if call == self.call:
            rows = None if future.cancelled() or future.exception() else future.result()
            if rows is None:
                self.failed = True
                self.on_ready()
                return
            self.done = (body, rows)
            self.on_ready()
        if self.wanted is not None and self.wanted != key and not self.failed:
            self._start()


def _live_rows(delta: Delta, path: str, body: str, width: int):
    """The preview's delta rendering, wrapped to `width` rows of fragments."""
    lines = delta.render(preview_patch(path, body), width, cache=False)
    if lines is None:
        return None
    # A console of its own: this runs on a worker thread.
    console = Console(width=width, color_system="truecolor", force_terminal=True)
    return [
        text_fragments(row, console)
        for line in lines
        for row in line.wrap(console, width, overflow="fold", no_wrap=False)
    ]


def _pending_rows(body: str, width: int, code_theme: str) -> list[tuple[str, str]]:
    """Lines delta has not rendered yet, laid out as delta will lay them out."""
    console = Console(width=width)
    rows = []
    for line in body.split("\n"):
        if line[:1] not in ("-", "+"):
            continue
        style = _preview_style(code_theme, line.startswith("+"))
        wrapped = Text(line[1:]).wrap(console, width, overflow="fold", no_wrap=False)
        rows.extend((style, row.plain) for row in wrapped)
    return rows


def edit_preview_rows(text: str, width: int, code_theme: str) -> list[tuple[str, str]]:
    """Color logical +/- lines before wrapping so continuation rows keep their color."""
    width = max(1, width)
    console = Console(width=width)
    rows = []
    for line in command_text(text).split("\n"):
        style = (
            _preview_style(code_theme, line.startswith("+"))
            if line.startswith(("+", "-"))
            else "class:bottom-toolbar.text"
        )
        rows.extend(
            (style, row.plain)
            for row in Text(line).wrap(console, width, overflow="fold", no_wrap=False)
        )
    return rows
