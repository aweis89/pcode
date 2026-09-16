"""Editable prompt with append-only output in the terminal's normal scrollback."""

import asyncio
import re
from contextlib import contextmanager
from dataclasses import dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app, in_terminal
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.filters import Always, Condition, has_focus, is_searching
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.containers import VerticalAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.search import stop_search
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from pcode.commands import CommandRegistry, SlashCompleter
from pcode.runtime import Event, Message, ToolSummary
from pcode.tool_display import label, plain


@dataclass(frozen=True)
class Palette:
    accent: str
    muted: str
    surface: str
    foreground: str
    selected: str
    syntax: str

    def prompt_style(self) -> Style:
        return Style.from_dict(
            {
                "prompt": f"{self.accent} bold",
                "frame.border": self.muted,
                "bottom-toolbar": f"noreverse bg:{self.surface} {self.muted}",
                "bottom-toolbar.text": self.muted,
                "completion-menu.completion": f"bg:{self.surface} {self.foreground}",
                "completion-menu.completion.current": f"bg:{self.selected} {self.accent} bold",
                "completion-menu.meta.completion": f"bg:{self.surface} {self.muted}",
                "completion-menu.meta.completion.current": f"bg:{self.selected} {self.foreground}",
                "auto-suggestion": self.muted,
            }
        )


PALETTES = {
    "dark": Palette("#88c0d0", "#8994a6", "#242933", "#e5e9f0", "#384457", "nord"),
    "light": Palette("#006b80", "#586575", "#edf0f4", "#202630", "#d0e7ef", "friendly"),
}


@dataclass
class Activity:
    busy: bool = False
    text: str = ""
    status: str = ""
    queued: int = 0

    def preview(self):
        return [("", self.text)]


@Output.register
class CursorSafeOutput:
    """Delegate terminal operations, but hide the cursor during output handoffs."""

    def __init__(self, output: Output):
        self.output = output
        self._hidden = 0
        self._visible = True

    def __getattr__(self, name):
        return getattr(self.output, name)

    def show_cursor(self) -> None:
        self._visible = True
        if not self._hidden:
            self.output.show_cursor()

    def hide_cursor(self) -> None:
        self._visible = False
        self.output.hide_cursor()

    @contextmanager
    def hidden_cursor(self):
        self._hidden += 1
        self.output.hide_cursor()
        self.output.flush()
        try:
            yield
        finally:
            self._hidden -= 1
            if not self._hidden:
                if self._visible:
                    self.output.show_cursor()
                self.output.flush()


class TerminalOutput:
    """Commit completed Markdown blocks once; keep a bounded unfinished preview.

    All writes run through one batched terminal handoff. Never hold the handoff
    across a network await: the editor must keep receiving input while streaming.
    """

    def __init__(self, console: Console, activity: Activity, app: Application, *, code_theme=None):
        self.console = console
        self.activity = activity
        self.app = app
        self.tail = ""
        self.streamed = False
        self.code_theme = code_theme or (lambda: "nord")
        self.pending: list[tuple[tuple[object, ...], str, bool]] = []
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()

    def print(self, *objects) -> None:
        self.pending.append((objects, "\n", False))
        self.changed.set()

    def _commit(self, source: str) -> None:
        if source.strip():
            self.print(Markdown(source, code_theme=self.code_theme()))
            self.print()

    def delta(self, text: str) -> None:
        if not text:
            return
        self.streamed = True
        # Model output is text, never terminal control sequences.
        text = "".join(
            "    "
            if c == "\t"
            else c
            if c == "\n" or ord(c) >= 32 and not 127 <= ord(c) < 160
            else "�"
            for c in text
        )
        # Examine boundaries independently of provider chunk sizes. Do not parse
        # every token: a newline can complete a block, a partial line cannot.
        for part in text.splitlines(keepends=True):
            self.tail += part
            if part.endswith("\n"):
                self._commit_blocks()
        self.changed.set()

    def _commit_blocks(self) -> None:
        lines = self.tail.splitlines(keepends=True)
        tokens = Markdown(self.tail).parsed
        blocks = [token for token in tokens if token.level == 0 and token.map]
        if not blocks:
            return
        last = blocks[-1]
        # Retain the last container: blank lines can belong to lists, quotes,
        # indented code, or fenced code. A following top-level block settles it.
        end = last.map[0]
        if lines[-1].strip() == "" and last.type in {
            "paragraph_open",
            "heading_open",
            "table_open",
            "hr",
        }:
            end = len(lines)
        elif last.type == "fence":
            closing = lines[last.map[1] - 1].rstrip("\r\n")
            if last.map[1] - last.map[0] > 1 and re.fullmatch(
                r" {0,3}" + re.escape(last.markup[0]) + "{" + str(len(last.markup)) + r",} *",
                closing,
            ):
                end = last.map[1]
        if end:
            self._commit("".join(lines[:end]))
            self.tail = "".join(lines[end:])

    def _preview(self) -> str:
        # Only one display row belongs to prompt_toolkit. Keep the complete
        # source separately so clipping never loses text from final scrollback.
        width = max(1, self.app.output.get_size().columns)
        rows = Text(self.tail).wrap(self.console, width, overflow="fold")
        return rows[-1].plain if rows else ""

    def finish(self, fallback: str = "") -> None:
        # Message is a completion marker, not a second copy of streamed text.
        if not self.streamed and fallback:
            self.delta(fallback)
        if self.streamed:
            self._commit(self.tail)
        self.tail = ""
        self.streamed = False
        self.changed.set()

    async def flush(self) -> None:
        async with self.lock:
            if self.pending:
                # Renderer.reset() shows the cursor at the transcript position
                # both when erasing and before repainting. Suppress those shows
                # until in_terminal has restored the editor and its cursor.
                with self.app.output.hidden_cursor():
                    async with in_terminal():
                        # Snapshot after entering: input/model events can arrive while
                        # in_terminal waits for CPR, but not during these sync writes.
                        pending, self.pending = self.pending, []
                        self.activity.text = self._preview()
                        for objects, end, soft_wrap in pending:
                            self.console.print(
                                *objects,
                                end=end,
                                soft_wrap=soft_wrap,
                                width=max(1, self.app.output.get_size().columns),
                            )
            else:
                self.activity.text = self._preview()
            self.changed.clear()
            self.app.invalidate()

    async def run(self) -> None:
        while True:
            await self.changed.wait()
            await asyncio.sleep(1 / 30)
            await self.flush()


def create_prompt(
    registry: CommandRegistry,
    *,
    activity: Activity | None = None,
    transcript: "Transcript | None" = None,
    on_submit=None,
    on_cancel=None,
    **kwargs,
) -> PromptSession:
    activity = activity or Activity()
    keys = KeyBindings()

    @keys.add("enter", filter=~is_searching)
    def submit(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if buffer.complete_state and buffer.complete_state.current_completion:
            # First Enter accepts the selected completion; next Enter sends it.
            buffer.complete_state = None
        else:
            buffer.validate_and_handle()

    @keys.add("escape", "enter", filter=~is_searching)
    def newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @keys.add("c-d", filter=Condition(lambda: activity.busy))
    def cancel(event: KeyPressEvent) -> None:
        event.app.exit(exception=KeyboardInterrupt)

    if transcript is not None:

        @keys.add("c-c")
        @keys.add("c-d", filter=Condition(lambda: activity.busy))
        def interrupt(event):
            if activity.busy:
                on_cancel()
            else:
                if is_searching():
                    stop_search()
                session.default_buffer.reset()
                transcript.note("Input discarded. Ctrl+D on an empty prompt exits.")

        @keys.add("c-d", filter=Condition(lambda: not activity.busy))
        def exit_or_delete(event):
            if not event.current_buffer.text:
                event.app.exit()
            else:
                event.current_buffer.delete()

    output = kwargs.pop("output", None)
    if not isinstance(output, CursorSafeOutput):
        output = CursorSafeOutput(output if output is not None else create_output())
    session = PromptSession(
        output=output,
        message=[("class:prompt", "❯ ")],
        prompt_continuation=lambda width, line, soft: [("class:prompt", "  " if soft else "· ")],
        multiline=True,
        erase_when_done=True,
        completer=SlashCompleter(registry),
        complete_while_typing=Condition(
            lambda: (
                get_app().current_buffer.text.startswith("/")
                and "\n" not in get_app().current_buffer.text
            )
        ),
        reserve_space_for_menu=0,
        auto_suggest=AutoSuggestFromHistory(),
        key_bindings=keys,
        mouse_support=False,
        **kwargs,
    )

    # Retain PromptSession's editor/processors, but give its frame a content-sized
    # height. The default frame expands into the CPR-reported space below the
    # cursor, which can be almost the whole pane after a tmux split.
    editor = session.layout.current_window
    editor.height = None
    editor.dont_extend_height = Always()
    search = ConditionalContainer(
        Window(editor.content.search_buffer_control, height=1, style="class:search-toolbar"),
        filter=is_searching,
    )

    def frame_height() -> int:
        size = session.app.output.get_size()
        available = max(1, size.rows - 4)
        text_height = editor.preferred_height(max(1, size.columns - 2), available).preferred
        return min(text_height, available) + 2

    menu = CompletionsMenu(
        max_height=6, scroll_offset=1, extra_filter=has_focus(session.default_buffer)
    )
    menu.content.dont_extend_height = Always()
    live = ConditionalContainer(
        Window(
            FormattedTextControl(activity.preview, show_cursor=False),
            wrap_lines=True,
            dont_extend_height=True,
        ),
        filter=Condition(lambda: bool(activity.text)),
    )
    # The unfinished line belongs directly after committed output, not in a
    # preview beside the editor. Put spare height BELOW it to avoid a jump when
    # that line is committed to scrollback. The editor stays bottom-aligned.
    children = [live, menu, search, Frame(editor, height=frame_height)]
    if transcript is not None:
        children.insert(1, Window())

        def accept(buffer):
            text = buffer.text
            buffer.append_to_history()
            on_submit(text)
            return False

        session.default_buffer.accept_handler = accept
    if session.bottom_toolbar is not None:
        children.append(
            Window(
                FormattedTextControl(
                    lambda: session.bottom_toolbar, style="class:bottom-toolbar.text"
                ),
                style="class:bottom-toolbar",
                height=1,
            )
        )
    session.layout = Layout(
        HSplit(children, align=VerticalAlign.JUSTIFY if transcript else VerticalAlign.BOTTOM),
        focused_element=editor,
    )
    session.app.layout = session.layout
    if transcript is not None:
        editor_app = session.app
        session.app = Application(
            layout=session.layout,
            full_screen=False,
            erase_when_done=True,
            min_redraw_interval=1 / 30,
            key_bindings=editor_app.key_bindings,
            style=editor_app.style,
            input=editor_app.input,
            output=editor_app.output,
            mouse_support=False,
        )
    return session


class Transcript:
    def __init__(self, console: Console, theme: str = "dark") -> None:
        self.console = console
        self.theme = theme
        self.output: TerminalOutput | None = None

    def print(self, *objects) -> None:
        if self.output is not None:
            self.output.print(*objects)
        else:
            self.console.print(*objects)

    @property
    def palette(self) -> Palette:
        return PALETTES[self.theme]

    def welcome(self, model: str | None = None, workspace: str = "") -> None:
        self.print()
        self.print(
            Text.assemble(
                ("pcode", f"bold {self.palette.accent}"),
                (f"  /  {model or 'UI preview'}", self.palette.muted),
            )
        )
        if model:
            self.note(f"Coder · workspace: {workspace}")
            self.note("Live model · file edits and shell tools enabled · not a sandbox")
        else:
            self.note("Local only · no model connected · no files or shell tools")
        self.note("Type / for commands, /demo for a sample response, /help for keys.")
        self.print()

    def note(self, text: str) -> None:
        self.print(Text(text, style=self.palette.muted))

    def user(self, text: str) -> None:
        self.print(Text.assemble(("❯ ", f"bold {self.palette.accent}"), text))
        self.print()

    def events(self, events: tuple[Event, ...]) -> None:
        for event in events:
            if isinstance(event, Message):
                self.print(Markdown(event.markdown, code_theme=self.palette.syntax))
                self.print()
            elif isinstance(event, ToolSummary):
                self.print(
                    Text.assemble(
                        (
                            f"  {'!' if event.failed else '✓'} {label(event.name)}  ",
                            "bold red" if event.failed else self.palette.accent,
                        ),
                        (plain(event.detail, limit=None), self.palette.muted),
                        (
                            f"  {event.elapsed_seconds:.1f}s"
                            if event.elapsed_seconds is not None
                            else "",
                            self.palette.muted,
                        ),
                    )
                )

    def help(self, registry: CommandRegistry) -> None:
        table = Table(box=None, padding=(0, 2), show_header=False)
        table.add_column(style=self.palette.accent, no_wrap=True)
        table.add_column()
        for command in registry.commands:
            table.add_row(command.name, command.description)
        self.print(table)
        self.print()
        self.note("Enter send · Alt+Enter newline (or Esc, Enter) · Tab/↑/↓ complete")
        self.note("Enter accepts a selected completion; press again to send.")
        self.note("Ctrl+R search history · Ctrl+C discard input · Ctrl+D exit on empty input")
        self.note("During a run: type a draft · Enter queues · Ctrl+C/Ctrl+D cancel, keep draft.")
        self.note("Cancellation clears queued messages. Use terminal/tmux scrollback for history.")
        self.print()
