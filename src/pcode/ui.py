"""Editable prompt with append-only output in the terminal's normal scrollback."""

import asyncio
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cache
from time import monotonic

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app, in_terminal
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Always, Condition, has_focus, is_searching, vi_mode
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import VerticalAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.renderer import Renderer
from prompt_toolkit.search import stop_search
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, Label
from rich.console import Console
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from pcode.commands import CommandRegistry, SlashCompleter
from pcode.preferences import load_preferences
from pcode.runtime import Event, Message, ToolSummary
from pcode.task_prompt import TaskPrompt
from pcode.theme import detect_theme
from pcode.tool_display import command_preview, command_text, label, plain
from pcode.tool_panel import ToolHistory, panel_fragments, task_panel_rows
from pcode.transcript_notice import TranscriptNotice


@dataclass(frozen=True)
class Palette:
    accent: str
    muted: str
    surface: str
    foreground: str
    selected: str
    syntax: str

    @cache
    def rich_theme(self) -> Theme:
        # Body text/background remain terminal-native; accents and bounded code
        # surfaces are explicitly paired for the selected appearance.
        return Theme(
            {
                "pcode.accent": self.accent,
                "pcode.brand": f"bold {self.accent}",
                "pcode.muted": self.muted,
                "pcode.error": "bold red",
                "pcode.warning": "bold yellow",
                "markdown.code": f"{self.foreground} on {self.surface}",
                "markdown.code_block": self.foreground,
                "markdown.block_quote": f"italic {self.muted}",
                "markdown.h1": f"bold underline {self.accent}",
                "markdown.h2": f"bold {self.accent}",
                "markdown.h3": f"bold {self.accent}",
                "markdown.h4": f"italic {self.accent}",
                "markdown.h5": f"italic {self.accent}",
                "markdown.h6": self.muted,
                "markdown.h7": f"italic {self.muted}",
                "markdown.hr": self.muted,
                "markdown.link": f"underline {self.accent}",
                "markdown.link_url": f"underline {self.accent}",
                "markdown.list": self.accent,
                "markdown.item.number": self.accent,
                "markdown.table.border": self.muted,
                "markdown.table.header": f"bold {self.accent}",
            }
        )

    @cache
    def prompt_style(self) -> Style:
        # Palette is immutable. Reuse the Style so DynamicStyle's identity-based
        # invalidation hash changes only with the palette, not on every redraw.
        return Style.from_dict(
            {
                "plan": self.muted,
                "plan.active": f"{self.accent} bold",
                "tool.failed": self.muted,
                "prompt": f"{self.accent} bold",
                "activity.prompt": self.muted,
                "frame.border": self.muted,
                # Keep foreground and background paired with the terminal theme:
                # the app palette may still be dark on a light terminal.
                "bottom-toolbar": "noreverse nodim bg:default fg:default",
                "bottom-toolbar.text": "fg:default",
                "bottom-toolbar.location": "fg:default bold",
                "bottom-toolbar.model": "fg:default",
                "bottom-toolbar.activity": "fg:default bold",
                "completion-menu": f"bg:{self.surface} {self.foreground}",
                "completion-menu.completion": f"bg:{self.surface} {self.foreground}",
                # The toolkit's selected-row default uses reverse; explicitly
                # disable it so light themes keep dark text on a light surface.
                "completion-menu.completion.current": (
                    f"noreverse bg:{self.selected} {self.accent} bold"
                ),
                "completion-menu scrollbar.background": f"bg:{self.surface}",
                "completion-menu scrollbar.button": f"bg:{self.selected}",
                "completion-menu.meta.completion": f"bg:{self.surface} {self.muted}",
                "completion-menu.meta.completion.current": f"bg:{self.selected} {self.foreground}",
                "auto-suggestion": self.muted,
            }
        )


PALETTES = {
    "dark": Palette("#88c0d0", "#8994a6", "#242933", "#e5e9f0", "#384457", "nord"),
    "light": Palette("#006b80", "#586575", "#edf0f4", "#202630", "#d0e7ef", "friendly"),
}


COLOR_STYLES = ("palette", "terminal")


# Rich owns scrollback, not the prompt palette. Use terminal-defined ANSI colors
# and leave the background alone so output fits either terminal appearance.
# In particular, Rich's default inline code paints a black background.
TERMINAL_THEME = Theme(
    {
        "pcode.accent": "cyan",
        "pcode.brand": "bold cyan",
        "pcode.muted": "default",
        "pcode.error": "bold red",
        "pcode.warning": "bold yellow",
        "markdown.code": "bold cyan",
        "markdown.code_block": "default",
        "markdown.block_quote": "italic default",
        "markdown.h1": "bold underline",
        "markdown.h2": "bold",
        "markdown.h3": "bold cyan",
        "markdown.h4": "italic cyan",
        "markdown.h5": "italic",
        "markdown.h6": "italic",
        "markdown.h7": "italic",
        "markdown.hr": "default",
        "markdown.link": "underline blue",
        "markdown.link_url": "underline blue",
        "markdown.table.border": "cyan",
        "markdown.table.header": "bold",
    }
)


@dataclass
class Activity:
    show_thinking: bool = False
    thinking: str = ""
    busy: bool = False
    status: str = ""
    queued: int = 0
    queued_prompts: list[str] = field(default_factory=list)
    prompt: str = ""
    prompt_state: str = ""
    plan: list[dict] = field(default_factory=list)
    plan_preview: list[dict] | None = None
    tools: ToolHistory = field(default_factory=ToolHistory)

    def append_thinking(self, text: str) -> None:
        """UI-only rolling buffer; never route this through Transcript/events."""
        self.thinking = (self.thinking + text)[-8192:]

    def thinking_rows(self) -> list[tuple[str, str]]:
        if not self.show_thinking or not self.thinking:
            return []
        # Plain, single-line preview: no terminal controls or provider metadata.
        return [
            (
                "class:bottom-toolbar.text",
                "Thinking · Ctrl+T to hide: " + plain(self.thinking[-240:], limit=None),
            )
        ]

    def reset(self) -> None:
        """Clear the panel for a new conversation, keeping the draft and queue."""
        self.thinking = ""
        self.plan = []
        self.plan_preview = None
        self.tools.clear()
        self.prompt = ""
        self.prompt_state = ""
        self.status = ""

    @property
    def displayed_plan(self) -> list[dict]:
        return self.plan if self.plan_preview is None else self.plan_preview

    def plan_rows(self, budget: int, spinner: str):
        # Persisted task status describes unfinished work, not a live request.
        # Use the turn lifecycle rather than busy, which also includes queued input.
        icon = spinner if self.prompt_state == "running" else "○"
        return task_panel_rows(self.displayed_plan, self.tools, budget, icon)

    def panel_title(self) -> str:
        items = self.displayed_plan
        if not items:
            return "Tools"
        completed = sum(item.get("status") == "completed" for item in items)
        return f"Tasks {completed}/{len(items)}"

    def prompt_fragments(self, spinner: str, width: int):
        icons = {"running": spinner, "failed": "!", "cancelled": "■", "done": "✓"}
        style = "class:activity.prompt"
        suffix = {"failed": " · failed", "cancelled": " · cancelled"}.get(self.prompt_state, "")
        # Measure terminal cells, not characters, so wide Unicode fits too.
        prefix = Text(icons.get(self.prompt_state, "❯") + " ")
        prefix.truncate(max(0, width), overflow="crop")
        text = Text(plain(self.prompt, limit=None) + suffix)
        remaining = max(0, width - prefix.cell_len)
        text.truncate(remaining, overflow="ellipsis" if remaining else "crop")
        return [(style, prefix.plain), (style, text.plain)]

    def queue_rows(self, budget: int):
        """Show the next queued prompts, leaving room for the editor on short panes."""
        if budget <= 0:
            return []
        visible = budget if len(self.queued_prompts) <= budget else budget - 1
        rows = [("class:plan", f"Queued: {text}") for text in self.queued_prompts[:visible]]
        remaining = len(self.queued_prompts) - visible
        if remaining > 0:
            rows.append(("class:plan", f"… {remaining} more queued"))
        return rows


@Output.register
class CursorSafeOutput:
    """Keep renderer erases out of history and hide the cursor during handoffs."""

    def __init__(self, output: Output):
        self.output = output
        self._hidden = 0
        self._visible = True

    def __getattr__(self, name):
        return getattr(self.output, name)

    def erase_down(self) -> None:
        # Avoid ED at column zero: terminals such as tmux preserve a full-screen
        # erase in scrollback, including the supposedly transient editor frame.
        # prompt_toolkit calls this at column zero. Split the erase into ED from
        # column one plus EL from column zero, keeping the final cursor unchanged.
        if self.output.get_size().columns < 2:
            self.output.erase_down()
            return
        self.output.cursor_forward(1)
        self.output.erase_down()
        self.output.cursor_backward(1)
        self.output.erase_end_of_line()

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


class ReflowAwareRenderer(Renderer):
    """Erase every physical row tmux produced from the last layout on narrowing.

    prompt_toolkit paints full-width rows with autowrap disabled, so tmux does
    not flag them as wrapped. On a narrowing resize tmux still splits any row
    whose used cells exceed the new width, before the app sees SIGWINCH, and
    keeps the cursor inside its split row. The stock erase then moves up by the
    old logical row count and leaves the top of the old layout on screen. Count
    the split rows instead. Partial clears never shrink tmux's used-cell count;
    only the column-zero full erase does, so track the widest write per row
    since the last erase.
    """

    def __init__(self, *args, **kwargs) -> None:
        self._row_extents: dict[int, int] = {}
        super().__init__(*args, **kwargs)

    def reset(self, _scroll: bool = False, leave_alternate_screen: bool = True) -> None:
        self._row_extents = {}
        super().reset(_scroll, leave_alternate_screen)

    def render(self, app, layout, is_done: bool = False) -> None:
        super().render(app, layout, is_done)
        screen, size, has_style = self._last_screen, self._last_size, self._style_string_has_style
        if screen is None or size is None or has_style is None:
            return
        for y in range(screen.height):
            extent = 0
            for x, cell in screen.data_buffer[y].items():
                if cell.char != " " or has_style[cell.style]:
                    extent = max(extent, min(x, size.columns - 1) + (cell.width or 1))
            if extent > self._row_extents.get(y, 0):
                self._row_extents[y] = extent

    def _reflowed_rows_above_cursor(self) -> int | None:
        """Physical rows above the cursor after tmux narrowed the pane, else None."""
        if not os.environ.get("TMUX") or self._last_size is None or self._in_alternate_screen:
            return None
        columns = self.output.get_size().columns
        if columns < 1 or columns >= self._last_size.columns:
            return None
        cursor = self._cursor_pos
        rows = 0
        for y in range(cursor.y):
            rows += max(1, -(-self._row_extents.get(y, 0) // columns))
        extent = self._row_extents.get(cursor.y, 0)
        if extent > columns:
            # tmux keeps the cursor in its split piece, or after the last one.
            rows += cursor.x // columns if cursor.x < extent else (extent - 1) // columns
        return rows

    def erase(self, leave_alternate_screen: bool = True) -> None:
        rows = self._reflowed_rows_above_cursor()
        if rows is None:
            super().erase(leave_alternate_screen)
            return
        output = self.output
        output.cursor_backward(self._cursor_pos.x)
        output.cursor_up(rows)
        output.erase_down()
        output.reset_attributes()
        output.enable_autowrap()
        output.flush()
        self.reset(leave_alternate_screen=leave_alternate_screen)


def install_reflow_renderer(app: Application) -> None:
    app.renderer = ReflowAwareRenderer(
        app._merged_style,
        app.output,
        full_screen=False,
        mouse_support=False,
        cpr_not_supported_callback=app.cpr_not_supported_callback,
    )


class TerminalOutput:
    """Commit completed Markdown blocks once; buffer unfinished text.

    All writes run through one batched terminal handoff. Never hold the handoff
    across a network await: the editor must keep receiving input while streaming.
    """

    def __init__(
        self,
        console: Console,
        app: Application,
        *,
        code_theme=None,
        rich_theme=None,
    ):
        self.console = console
        self.app = app
        self.tail = ""
        self.streamed = False
        self._turn_prompt: str | None = None
        self.code_theme = code_theme or (lambda: PALETTES["dark"].syntax)
        self.rich_theme = rich_theme or PALETTES["dark"].rich_theme
        self.pending: list[tuple[tuple[object, ...], str, bool]] = []
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()

    def print(self, *objects) -> None:
        self.pending.append((objects, "\n", False))
        self.changed.set()

    def begin_turn(self, prompt: str) -> None:
        # A waiting turn already has a live prompt above the activity panel.
        # Keep its scrollback quote attached to the first visible model block,
        # not the first token (which may remain buffered for a while).
        self._turn_prompt = prompt

    def end_turn(self) -> None:
        self.finish()
        # Empty, failed, or cancelled turns must not leak a quote into a later turn.
        self._turn_prompt = None

    def _commit(self, source: str) -> None:
        if source.strip():
            if self._turn_prompt is not None:
                self.print()
                self.print(TaskPrompt(self._turn_prompt))
                self.print()
                self._turn_prompt = None
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
                        width = max(1, self.app.output.get_size().columns)
                        # Rich's public buffer context coalesces the batch's
                        # prints (including separators) into one output flush.
                        # Keep it synchronous and inside the single-writer handoff.
                        with self.console, self.console.use_theme(self.rich_theme()):
                            for objects, end, soft_wrap in pending:
                                self.console.print(
                                    *objects, end=end, soft_wrap=soft_wrap, width=width
                                )
                # in_terminal already repainted the editor on exit.
            self.changed.clear()

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
    on_effort=None,
    on_model=None,
    on_thinking=None,
    **kwargs,
) -> PromptSession:
    activity = activity or Activity()
    keys = KeyBindings()

    @keys.add("c-t", filter=~is_searching)
    def toggle_thinking(event: KeyPressEvent) -> None:
        activity.show_thinking = not activity.show_thinking
        if on_thinking is not None:
            on_thinking(activity.show_thinking)
        event.app.invalidate()

    if on_model is not None:

        @keys.add("c-l", filter=~is_searching)
        def choose_model(event: KeyPressEvent) -> None:
            on_model()

    if on_effort is not None:

        @keys.add("c-n", filter=~is_searching)
        def increase_effort(event: KeyPressEvent) -> None:
            on_effort(1)
            event.app.invalidate()

        @keys.add("c-p", filter=~is_searching)
        def decrease_effort(event: KeyPressEvent) -> None:
            on_effort(-1)
            event.app.invalidate()

    @keys.add("enter", filter=~is_searching)
    def submit(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if buffer.complete_state and buffer.complete_state.current_completion:
            # First Enter accepts the selected completion; next Enter sends it.
            buffer.complete_state = None
        else:
            buffer.validate_and_handle()

    @keys.add("escape", filter=vi_mode & ~is_searching, eager=True)
    def normal_mode(event: KeyPressEvent) -> None:
        # Match native vi Escape semantics, without waiting for Alt bindings.
        buffer = event.current_buffer
        state = event.app.vi_state
        if state.input_mode in (InputMode.INSERT, InputMode.REPLACE):
            buffer.cursor_position += buffer.document.get_cursor_left_position()
        state.input_mode = InputMode.NAVIGATION
        if buffer.selection_state:
            buffer.exit_selection()

    @keys.add("c-j", filter=~is_searching)
    @keys.add("escape", "enter", filter=~vi_mode & ~is_searching)
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
        available = max(1, size.rows - 4 - activity_height() - len(queue_rows()))
        text_height = editor.preferred_height(max(1, size.columns - 2), available).preferred
        return min(text_height, available) + 2

    plan_spinner = Spinner("arc")
    # Give the prompt line its own glyph so it reads as the overall turn, not as
    # another in-progress task row.
    prompt_spinner = Spinner("dots")
    # Animate active tasks and update running tool elapsed times during pauses.
    session.app.refresh_interval = min(plan_spinner.interval, prompt_spinner.interval) / 1000

    def plan_rows():
        # Share one height budget instead of stacking separate Tools and Tasks
        # panels. Leave space for the completion menu and editor.
        budget = min(10, max(1, session.app.output.get_size().rows // 2 - 2))
        return activity.plan_rows(budget, plan_spinner.render(monotonic()).plain)

    def activity_height() -> int:
        rows = plan_rows()
        return (
            bool(activity.prompt) + (len(rows) + 2 if rows else 0) + len(activity.thinking_rows())
        )

    def plan_text():
        return panel_fragments(plan_rows(), session.app.output.get_size().columns - 2)

    current_prompt = ConditionalContainer(
        Window(
            FormattedTextControl(
                lambda: activity.prompt_fragments(
                    prompt_spinner.render(monotonic()).plain,
                    session.app.output.get_size().columns,
                ),
                show_cursor=False,
            ),
            height=1,
            wrap_lines=False,
            dont_extend_height=True,
        ),
        filter=Condition(lambda: bool(activity.prompt)),
    )
    plan_frame = Frame(
        Window(
            FormattedTextControl(plan_text),
            height=lambda: len(plan_rows()),
            dont_extend_height=True,
            wrap_lines=False,
        ),
        height=lambda: len(plan_rows()) + 2,
    )
    # Frame centers its title and has no alignment option. Replace only its
    # top border with a fixed left prefix and an expanding right border.
    plan_frame.container.children[0] = VSplit(
        [
            Window(FormattedTextControl("┌─ "), width=3, style="class:frame.border"),
            Label(
                lambda: panel_fragments(
                    [("bold", activity.panel_title())],
                    session.app.output.get_size().columns - 8,
                ),
                style="class:frame.label",
                dont_extend_width=True,
            ),
            Window(FormattedTextControl(" "), width=1, style="class:frame.border"),
            Window(char="─", style="class:frame.border"),
            Window(char="┐", width=1, style="class:frame.border"),
        ],
        height=1,
    )
    plan = ConditionalContainer(plan_frame, filter=Condition(lambda: bool(plan_rows())))
    # Keep the turn and its activity adjacent even when the root layout justifies
    # the transcript and editor across the remaining terminal height.
    thinking = ConditionalContainer(
        Window(
            FormattedTextControl(lambda: activity.thinking_rows(), show_cursor=False),
            height=1,
            dont_extend_height=True,
            wrap_lines=False,
        ),
        filter=Condition(lambda: bool(activity.thinking_rows())),
    )
    activity_panel = HSplit([current_prompt, plan, thinking])

    def queue_rows():
        budget = min(4, max(1, session.app.output.get_size().rows // 4))
        return activity.queue_rows(budget)

    queued = ConditionalContainer(
        Window(
            FormattedTextControl(
                lambda: panel_fragments(queue_rows(), session.app.output.get_size().columns),
                show_cursor=False,
            ),
            height=lambda: len(queue_rows()),
            wrap_lines=False,
            dont_extend_height=True,
        ),
        filter=Condition(lambda: bool(activity.queued_prompts)),
    )
    menu = CompletionsMenu(
        max_height=6, scroll_offset=1, extra_filter=has_focus(session.default_buffer)
    )
    menu.content.dont_extend_height = Always()
    children = [menu, search, activity_panel, queued, Frame(editor, height=frame_height)]
    if transcript is not None:
        children.insert(0, Window())

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
            refresh_interval=editor_app.refresh_interval,
            key_bindings=editor_app.key_bindings,
            editing_mode=editor_app.editing_mode,
            style=editor_app.style,
            input=editor_app.input,
            output=editor_app.output,
            mouse_support=False,
        )
    if session.app.editing_mode == EditingMode.VI:
        # Allow terminal escape sequences to arrive, without a half-second pause.
        session.app.ttimeoutlen = 0.1
    install_reflow_renderer(session.app)

    return session


class Transcript:
    """Persistent Rich output: anything written here belongs in terminal scrollback.

    Mutable activity and event routing belong to the application, not this writer.
    """

    def __init__(
        self,
        console: Console,
        theme: str = "dark",
        *,
        activity: Activity | None = None,
        color_style: str = "palette",
    ) -> None:
        preferences = load_preferences()
        self.error_scrollback = preferences.get("error_scrollback", "on") == "on"
        self.error_scrollback_lines = int(preferences.get("error_scrollback_lines", "20"))
        self.activity = activity
        self.console = console
        self.theme = theme
        self.detected_theme = detect_theme()
        self.color_style = color_style
        self.output: TerminalOutput | None = None

    def print(self, *objects) -> None:
        if self.output is not None:
            self.output.print(*objects)
        else:
            with self.console.use_theme(self.rich_theme):
                self.console.print(*objects)

    @property
    def resolved_theme(self) -> str:
        return self.detected_theme if self.theme == "auto" else self.theme

    @property
    def palette(self) -> Palette:
        return PALETTES[self.resolved_theme]

    @property
    def rich_theme(self) -> Theme:
        return TERMINAL_THEME if self.color_style == "terminal" else self.palette.rich_theme()

    @property
    def code_theme(self) -> str:
        return (
            f"ansi_{self.resolved_theme}" if self.color_style == "terminal" else self.palette.syntax
        )

    def welcome(self, model: str | None = None, workspace: str = "") -> None:
        self.print()
        self.print(
            Text.assemble(
                ("pcode", "pcode.brand"),
                (f"  /  {model or 'UI preview'}", "pcode.muted"),
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
        self.print(Text(text, style="pcode.muted"))

    def error(self, text: str, *, title: str = "Error") -> None:
        if self.error_scrollback:
            self.print(
                TranscriptNotice(text, "error", title, self.error_scrollback_lines, self.code_theme)
            )

    def warning(self, text: str) -> None:
        self.print(TranscriptNotice(text, "warning", "Warning"))

    def cancelled(self) -> None:
        self.print(
            TranscriptNotice("Completed tool effects are not undone.", "cancelled", "Run cancelled")
        )

    def user(self, text: str) -> None:
        self.print()
        self.print(TaskPrompt(text))
        self.print()

    def command_summary(self, event: ToolSummary) -> None:
        result = (
            " · " + plain(event.detail.rsplit(" → ", 1)[-1], limit=60)
            if event.failed or (event.name != "run_command" and " → " in event.detail)
            else ""
        )
        elapsed = f" · {event.elapsed_seconds:.1f}s" if event.elapsed_seconds is not None else ""
        header = Text(
            f"  {'!' if event.failed else '✓'} {label(event.name)}{result}{elapsed}",
            style="pcode.accent",
            no_wrap=True,
            overflow="ellipsis",
        )
        preview = Text(
            "    " + command_preview(event.command),
            style="pcode.muted",
            no_wrap=True,
            overflow="ellipsis",
        )
        header.truncate(self.console.width, overflow="ellipsis")
        preview.truncate(self.console.width, overflow="ellipsis")
        self.print(header)
        self.print(preview)

    def events(self, events: tuple[Event, ...], *, show_tools: bool = False) -> None:
        for event in events:
            if isinstance(event, Message):
                self.print(Markdown(event.markdown, code_theme=self.code_theme))
                self.print()
            elif isinstance(event, ToolSummary):
                if event.failed:
                    detail = (
                        command_preview(event.command)
                        if event.command
                        else plain(event.detail, limit=None)
                    )
                    if event.error:
                        detail += "\n" + "\n".join(
                            plain(line, limit=None) for line in event.error.splitlines()
                        )
                    self.error(detail, title=f"{label(event.name)} failed")
                    continue
                if event.command:
                    if show_tools:
                        self.print(Text(f"{label(event.name)} · {plain(event.detail, limit=None)}"))
                        self.print(Text(command_text(event.command)))
                    else:
                        self.command_summary(event)
                    continue
                self.print(
                    Text.assemble(
                        (
                            f"  {'!' if event.failed else '✓'} {label(event.name)}  ",
                            "pcode.accent",
                        ),
                        (plain(event.detail, limit=None), "pcode.muted"),
                        (
                            f"  {event.elapsed_seconds:.1f}s"
                            if event.elapsed_seconds is not None
                            else "",
                            "pcode.muted",
                        ),
                    )
                )

    def help(self, registry: CommandRegistry) -> None:
        table = Table(box=None, padding=(0, 2), show_header=False)
        table.add_column(style="pcode.accent", no_wrap=True)
        table.add_column()
        for command in registry.commands:
            table.add_row(command.name, command.description)
        self.print(table)
        self.print()
        self.note("/ commands · Enter send · Alt+Enter newline (or Esc, Enter) · Tab/↑/↓ complete")
        self.note("Enter accepts a selected completion; press again to send.")
        self.note("Ctrl+T show/hide transient thinking (saves default)")
        self.note("Ctrl+L choose model (keep conversation)")
        self.note("Ctrl+N increase effort · Ctrl+P decrease effort (next turn)")
        self.note("Ctrl+R search history · Ctrl+C discard input · Ctrl+D exit on empty input")
        self.note("During a run: type a draft · Enter queues · Ctrl+C/Ctrl+D cancel, keep draft.")
        self.note("Cancellation clears queued messages. Use terminal/tmux scrollback for history.")
        self.print()
