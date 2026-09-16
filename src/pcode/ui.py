"""Full-screen editing and width-aware Rich transcript rendering."""

from dataclasses import dataclass
from io import StringIO

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.filters import Always, Condition, has_focus, is_searching
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.containers import VerticalAlign
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.search import stop_search
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from pcode.commands import CommandRegistry, SlashCompleter
from pcode.runtime import Event, Message, ToolSummary


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

    def preview(self):
        # Keep the live tail bounded; completed messages remain in the transcript.
        return [("", self.text[-6000:] or self.status), ("[SetCursorPosition]", "")]


class TranscriptControl(UIControl):
    """Render retained Rich blocks at the actual pane width, not stdout width."""

    def __init__(self, transcript):
        self.transcript = transcript
        self.width = 0
        self.height = 1
        self.lines = []
        self.rendered = 0
        self.block_starts = []
        self.top = 0
        self.follow = True

    def create_content(self, width, height):
        anchor = None
        if width != self.width:
            if not self.follow and self.block_starts:
                block = max(i for i, start in enumerate(self.block_starts) if start <= self.top)
                end = (
                    self.block_starts[block + 1]
                    if block + 1 < len(self.block_starts)
                    else len(self.lines)
                )
                anchor = (
                    block,
                    (self.top - self.block_starts[block]) / max(1, end - self.block_starts[block]),
                )
            self.width = width
            self.lines = []
            self.block_starts = []
            self.rendered = 0
        self.height = height
        blocks = self.transcript.blocks
        for block in blocks[self.rendered :]:
            self.block_starts.append(len(self.lines))
            stream = StringIO()
            console = Console(
                file=stream,
                width=max(1, width),
                force_terminal=True,
                color_system="truecolor",
                legacy_windows=False,
            )
            console.print(*block)
            fragments = to_formatted_text(ANSI(stream.getvalue().rstrip("\n")))
            self.lines.extend(list(split_lines(fragments)))
        self.rendered = len(blocks)
        if anchor is not None:
            block, fraction = anchor
            start = self.block_starts[block]
            end = (
                self.block_starts[block + 1]
                if block + 1 < len(self.block_starts)
                else len(self.lines)
            )
            self.top = start + int(fraction * (end - start))
        maximum = max(0, len(self.lines) - height)
        if self.follow:
            self.top = maximum
        # Clamp the displayed viewport, not the reading anchor: shrinking again
        # after a height-only resize should restore the reader's position.
        visible_top = min(self.top, maximum)
        visible = self.lines[visible_top : visible_top + height]
        return UIContent(get_line=lambda i: visible[i], line_count=len(visible), show_cursor=False)

    def scroll(self, pages):
        self.follow = False
        maximum = max(0, len(self.lines) - self.height)
        self.top = max(0, min(maximum, min(self.top, maximum) + pages * max(1, self.height - 1)))
        if self.top == maximum:
            self.follow = True

    def latest(self):
        self.follow = True


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
        if activity.busy:
            return
        buffer = event.current_buffer
        if buffer.complete_state and buffer.complete_state.current_completion:
            # First Enter accepts the selected completion; next Enter sends it.
            buffer.complete_state = None
        else:
            buffer.validate_and_handle()

    @keys.add("escape", "enter", filter=~is_searching)
    def newline(event: KeyPressEvent) -> None:
        if not activity.busy:
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

        view = TranscriptControl(transcript)

        @keys.add("pageup")
        def page_up(event):
            view.scroll(-1)

        @keys.add("pagedown")
        def page_down(event):
            view.scroll(1)

        @keys.add("c-end")
        def latest(event):
            view.latest()

    session = PromptSession(
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
    session.default_buffer.read_only = Condition(lambda: activity.busy)
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
            height=lambda: Dimension(max=max(1, min(8, session.app.output.get_size().rows // 3))),
            dont_extend_height=True,
        ),
        filter=Condition(lambda: activity.busy),
    )
    # Keep transient output/menus above the editor so its bottom edge stays anchored.
    children = [live, menu, search, Frame(editor, height=frame_height)]
    if transcript is not None:
        children.insert(0, Window(view, wrap_lines=False))

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
            full_screen=True,
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
        self.full_screen = False
        self.blocks = []

    def print(self, *objects) -> None:
        self.blocks.append(objects)
        if not self.full_screen:
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
                        (f"  {'!' if event.failed else '✓'} {event.name}  ", self.palette.accent),
                        (event.detail, self.palette.muted),
                    )
                )
                self.print()

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
        self.note("During a run: Ctrl+C/Ctrl+D cancel; editing resumes when the run finishes.")
        self.note("Input history is in memory only. PgUp/PgDn scroll · Ctrl+End follow latest.")
        self.print()
