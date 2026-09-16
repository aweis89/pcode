"""prompt_toolkit owns editing; Rich prints completed transcript blocks once."""

from dataclasses import dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.filters import Always, Condition, has_focus, is_searching
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.containers import VerticalAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
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
        # Only the temporary tail is repainted. Full finalized blocks go to Rich.
        return [("", self.text[-6000:] or self.status), ("[SetCursorPosition]", "")]


def create_prompt(
    registry: CommandRegistry, *, activity: Activity | None = None, **kwargs
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
    session.layout = Layout(HSplit(children, align=VerticalAlign.BOTTOM), focused_element=editor)
    session.app.layout = session.layout
    return session


class Transcript:
    def __init__(self, console: Console, theme: str = "dark") -> None:
        self.console = console
        self.theme = theme

    @property
    def palette(self) -> Palette:
        return PALETTES[self.theme]

    def welcome(self, model: str | None = None, workspace: str = "") -> None:
        self.console.print()
        self.console.print(
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
        self.console.print()

    def note(self, text: str) -> None:
        self.console.print(Text(text, style=self.palette.muted))

    def user(self, text: str) -> None:
        self.console.print(Text.assemble(("❯ ", f"bold {self.palette.accent}"), text))
        self.console.print()

    def events(self, events: tuple[Event, ...]) -> None:
        for event in events:
            if isinstance(event, Message):
                self.console.print(Markdown(event.markdown, code_theme=self.palette.syntax))
                self.console.print()
            elif isinstance(event, ToolSummary):
                self.console.print(
                    Text.assemble(
                        (f"  {'!' if event.failed else '✓'} {event.name}  ", self.palette.accent),
                        (event.detail, self.palette.muted),
                    )
                )
                self.console.print()

    def help(self, registry: CommandRegistry) -> None:
        table = Table(box=None, padding=(0, 2), show_header=False)
        table.add_column(style=self.palette.accent, no_wrap=True)
        table.add_column()
        for command in registry.commands:
            table.add_row(command.name, command.description)
        self.console.print(table)
        self.console.print()
        self.note("Enter send · Alt+Enter newline (or Esc, Enter) · Tab/↑/↓ complete")
        self.note("Enter accepts a selected completion; press again to send.")
        self.note("Ctrl+R search history · Ctrl+C discard input · Ctrl+D exit on empty input")
        self.note("During a run: Ctrl+C/Ctrl+D cancel; editing resumes when the run finishes.")
        self.note("Input history is in memory only. Mouse selection stays with your terminal.")
        self.console.print()
