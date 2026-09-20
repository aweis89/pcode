"""Editable prompt with replayable output in the terminal's normal scrollback."""

import asyncio
import os
import re
from asyncio import Future
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field, replace
from functools import cache, lru_cache, wraps
from time import monotonic

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import merge_completers
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
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame, Label
from rich.console import Console
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from pcode.command_transcript import CommandTranscript
from pcode.commands import CommandRegistry, SlashCompleter
from pcode.edit_transcript import EditTranscript, edit_preview_rows
from pcode.file_refs import FileReferenceCompleter, ReferenceLexer, reference_fragment
from pcode.input_keys import configure_newline_keys
from pcode.preferences import SETTINGS, SYNTAX_THEMES, load_preferences
from pcode.runtime import CacheBust, CommandOutput, Event, Message, Thinking, ToolSummary
from pcode.syntax_colors import derive_colors
from pcode.task_prompt import TaskPrompt
from pcode.theme import detect_theme
from pcode.theme_gallery import SyntaxGallery
from pcode.thinking_markdown import ThinkingMarkdown
from pcode.tool_display import (
    COMMAND_TOOLS,
    EDIT_TOOLS,
    PLAN_TOOLS,
    command_preview,
    command_text,
    label,
    plain,
    tool_summary_lines,
)
from pcode.tool_panel import ToolHistory, panel_fragments, task_panel_rows
from pcode.transcript_log import RetainedMarkdown, TranscriptLog, recorded
from pcode.transcript_notice import TranscriptNotice
from pcode.word_wrap import WordWrapProcessor


@dataclass(frozen=True)
class Palette:
    accent: str
    muted: str
    surface: str
    foreground: str
    selected: str
    task_heading: str

    @cache
    def rich_theme(self) -> Theme:
        # Body text/background remain terminal-native; accents and bounded code
        # surfaces are explicitly paired for the selected appearance.
        return Theme(
            {
                "pcode.accent": self.accent,
                "pcode.brand": f"bold {self.accent}",
                "pcode.muted": self.muted,
                "pcode.thinking": f"dim {self.muted}",
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
    def prompt_style(self, menu: "Palette | None" = None) -> Style:
        # Palette is immutable. Reuse the Style so DynamicStyle's identity-based
        # invalidation hash changes only with the palette, not on every redraw.
        # `menu` colors the completion popup, which follows the selected syntax
        # style rather than this palette; it is immutable and cached too.
        menu = self if menu is None else menu
        return Style.from_dict(
            {
                "plan": self.muted,
                "plan.heading": f"{self.task_heading} bold",
                "plan.active": f"{self.accent} bold",
                "prompt": f"{self.accent} bold",
                "activity.prompt": self.muted,
                # System work is pcode's own, so it gets the accent colour and
                # an italic detail rather than the muted prompt echo styling.
                "activity.system": self.accent,
                "activity.system.label": f"{self.accent} bold",
                "activity.system.detail": f"italic {self.muted}",
                # Short-lived answers to a keystroke live above the spinner
                # rather than in scrollback; italics mark them as chrome.
                "activity.notice": f"italic {self.muted}",
                "frame.border": self.muted,
                "editor.mode": "noreverse nodim bg:#b8b8b8 fg:#ffffff",
                # Keep foreground and background paired with the terminal theme:
                # the app palette may still be dark on a light terminal.
                "bottom-toolbar": "noreverse nodim bg:default fg:default",
                "bottom-toolbar.text": "fg:default",
                "bottom-toolbar.location": "fg:default bold",
                "bottom-toolbar.model": "fg:default",
                "bottom-toolbar.activity": "fg:default bold",
                "completion-menu": f"bg:{menu.surface} {menu.foreground}",
                "completion-menu.completion": f"bg:{menu.surface} {menu.foreground}",
                # The toolkit's selected-row default uses reverse; explicitly
                # disable it so light themes keep dark text on a light surface.
                "completion-menu.completion.current": (
                    f"noreverse bg:{menu.selected} {menu.accent} bold"
                ),
                "completion-menu scrollbar.background": f"bg:{menu.surface}",
                "completion-menu scrollbar.button": f"bg:{menu.selected}",
                "completion-menu.meta.completion": f"bg:{menu.surface} {menu.muted}",
                "completion-menu.meta.completion.current": f"bg:{menu.selected} {menu.foreground}",
                # A file reference is neither prose nor a command: underlining
                # it marks the token without competing with the prompt chevron.
                "reference": f"{self.task_heading} underline",
                "auto-suggestion": self.muted,
            }
        )


PALETTES = {
    "dark": Palette("#88c0d0", "#8994a6", "#242933", "#e5e9f0", "#384457", "#c4b5fd"),
    "light": Palette("#006b80", "#586575", "#edf0f4", "#202630", "#d0e7ef", "#7c3aed"),
}


@cache
def syntax_palette(style_name: str, fallback: Palette, backdrop: str | None = None) -> Palette:
    """The palette a Pygments style implies, backed by `fallback`'s colors.

    `backdrop` is the background the colors will be painted on when it is not
    the style's own; see `derive_colors`.

    Cached because prompt_toolkit's DynamicStyle invalidates on the identity of
    the object it is handed: a fresh Palette on every redraw would rebuild the
    whole style tree. Palette is frozen, so it is a usable cache key.
    """
    return Palette(**derive_colors(style_name, asdict(fallback), backdrop))


def syntax_themes(preferences: dict[str, str] | None = None) -> dict[str, str]:
    """The saved Pygments style for each palette, by palette name.

    Fenced code keeps its own background, so the style has to be chosen per
    palette rather than derived from one: a dark style on a light terminal is
    readable but jarring, and `theme auto` can pick either at startup.
    """
    preferences = load_preferences() if preferences is None else preferences
    return {
        name: preferences.get(f"syntax_{name}", SETTINGS[f"syntax_{name}"].default)
        for name in PALETTES
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
        "pcode.thinking": "dim default",
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


# Marks rows pcode drives itself. The diamond reads as a system marker rather
# than the "❯" prompt chevron, and the arrows suggest folding history inward.
SYSTEM_BADGE = "◈"
SYSTEM_SEPARATOR = "▸"
# Slash commands pcode runs itself, labelled the same whether queued or running.
SYSTEM_COMMAND_LABELS = {"/compact": "Compacting context"}


def system_command(text: str) -> tuple[str, str] | None:
    """Split a slash command into its badge label and detail, or None if it is a prompt."""
    name, _, detail = plain(text, limit=None).strip().partition(" ")
    label = SYSTEM_COMMAND_LABELS.get(name)
    return (label, detail.strip()) if label else None


# A notice answers a keystroke, so it only has to outlast reading it once.
NOTICE_SECONDS = 5.0
NOTICE_ROWS = 6


@dataclass
class Activity:
    show_tasks: bool = True
    # Hide the widget again as soon as a turn ends, without forgetting that the
    # user wants it shown while the model works.
    autohide_tasks: bool = True
    tasks_autohidden: bool = False
    show_thinking: bool = False
    busy: bool = False
    status: str = ""
    queued: int = 0
    queued_prompts: list[str] = field(default_factory=list)
    queued_modes: list[str] = field(default_factory=list)
    prompt: str = ""
    prompt_state: str = ""
    # True while a `!command` typed at the prompt is running.
    user_command: bool = False
    # "user" echoes what was typed; "system" marks work pcode runs on its own
    # behalf (compaction, for example) so it never reads as part of the prompt.
    prompt_kind: str = "user"
    prompt_detail: str = ""
    plan: list[dict] = field(default_factory=list)
    plan_preview: list[dict] | None = None
    tools: ToolHistory = field(default_factory=ToolHistory)
    command_outputs: dict[str, CommandOutput] = field(default_factory=dict)
    edit_previews: dict = field(default_factory=dict)
    notice: str = ""
    notice_expires: float = 0.0

    def flash(self, text: str, seconds: float = NOTICE_SECONDS) -> None:
        """Replace the transient notice shown above the spinner.

        One slot, not a queue: the newest answer is the one being waited for,
        and stacking acknowledgements would push the editor down the screen.
        """
        self.notice = text
        self.notice_expires = monotonic() + seconds

    @property
    def notice_shown(self) -> bool:
        return bool(self.notice) and monotonic() < self.notice_expires

    def notice_rows(self, width: int) -> list[tuple[str, str]]:
        """Wrap the notice to the pane, bounded so chrome cannot take the screen."""
        if not self.notice_shown or width < 1:
            return []
        console = Console(width=width)
        rows = [
            ("class:activity.notice", row.plain)
            for line in self.notice.splitlines()
            for row in Text(plain(line, limit=None)).wrap(
                console, width, overflow="fold", no_wrap=False
            )
        ]
        return rows[:NOTICE_ROWS]

    def panel_heading(self) -> str:
        return self.panel_title()

    def reset(self) -> None:
        """Clear the panel for a new conversation, keeping the draft and queue."""
        self.command_outputs.clear()
        self.edit_previews.clear()
        self.plan = []
        self.plan_preview = None
        self.tools.clear()
        self.prompt = ""
        self.prompt_state = ""
        self.prompt_kind = "user"
        self.prompt_detail = ""
        self.status = ""
        self.tasks_autohidden = False

    @property
    def tasks_shown(self) -> bool:
        """Visible only when enabled and not auto-hidden after the last turn."""
        return self.show_tasks and not self.tasks_autohidden

    def toggle_tasks(self) -> bool:
        """Ctrl+O acts on what is on screen, so auto-hidden reads as hidden."""
        self.show_tasks = not self.tasks_shown
        self.tasks_autohidden = False
        return self.show_tasks

    def finish_prompt(self, state: str) -> None:
        """End the turn, auto-hiding the widget when that option is enabled."""
        self.prompt_state = state
        if self.autohide_tasks:
            self.tasks_autohidden = True

    def start_prompt(self, text: str, *, kind: str = "user", detail: str = "") -> None:
        """Show a running row, tagged so system work never looks like typed input."""
        self.tasks_autohidden = False
        self.prompt = text
        self.prompt_kind = kind
        self.prompt_detail = detail
        self.prompt_state = "running"

    @property
    def displayed_plan(self) -> list[dict]:
        return self.plan if self.plan_preview is None else self.plan_preview

    def plan_rows(self, budget: int, spinner: str):
        if not self.tasks_shown:
            return []
        # Persisted task status describes unfinished work, not a live request.
        # Use the turn lifecycle rather than busy, which also includes queued input.
        icon = spinner if self.status_shown else "○"
        return task_panel_rows(self.displayed_plan, self.tools, budget, icon)

    def panel_title(self) -> str:
        items = self.displayed_plan
        if not items:
            return "Tools"
        completed = sum(item.get("status") == "completed" for item in items)
        return f"Tasks {completed}/{len(items)}"

    @property
    def status_shown(self) -> bool:
        """The live row exists only while a turn runs; the prompt is in scrollback."""
        return self.prompt_state == "running"

    def status_fragments(self, spinner: str, width: int):
        """The row above the tasks: the spinner plus whatever is running right now."""
        if self.prompt_kind != "user":
            return self._system_fragments(spinner, width)
        call = self.tools.active
        style = "class:plan.active" if call else "class:activity.prompt"
        # Measure terminal cells, not characters, so wide Unicode fits too.
        prefix = Text(spinner + " ")
        prefix.truncate(max(0, width), overflow="crop")
        text = Text(call.line() if call else plain(self.status, limit=None) or "Working…")
        remaining = max(0, width - prefix.cell_len)
        text.truncate(remaining, overflow="ellipsis" if remaining else "crop")
        return [("class:activity.prompt", prefix.plain), (style, text.plain)]

    def _system_fragments(self, icon: str, width: int):
        """Render pcode's own work as a labelled badge, never as an echoed prompt."""
        prefix = Text(f"{icon} {SYSTEM_BADGE} ")
        prefix.truncate(max(0, width), overflow="crop")
        remaining = max(0, width - prefix.cell_len)
        label = Text(plain(self.prompt, limit=None))
        label.truncate(remaining, overflow="ellipsis" if remaining else "crop")
        fragments = [
            ("class:activity.system", prefix.plain),
            ("class:activity.system.label", label.plain),
        ]
        remaining = max(0, remaining - label.cell_len)
        detail = plain(self.prompt_detail, limit=None)
        if detail and remaining > 2:
            text = Text(f" {SYSTEM_SEPARATOR} {detail}")
            text.truncate(remaining, overflow="ellipsis")
            fragments.append(("class:activity.system.detail", text.plain))
        return fragments

    def queue_rows(self, budget: int):
        """Show the next queued prompts, leaving room for the editor on short panes."""
        if budget <= 0:
            return []
        visible = budget if len(self.queued_prompts) <= budget else budget - 1
        rows = []
        for index, text in enumerate(self.queued_prompts[:visible]):
            mode = self.queued_modes[index] if index < len(self.queued_modes) else "queue"
            prefix = {
                "steering": "Steering (next model request)",
                "interrupt": "Interrupting",
            }.get(mode, "Queued")
            system = system_command(text)
            if system is None:
                rows.append(("class:plan", f"{prefix}: {text}"))
                continue
            # Pending system work reads as a labelled action, matching the row
            # it becomes once it starts, rather than as an echoed command.
            label, detail = system
            body = f"{SYSTEM_BADGE} {label}"
            if detail:
                body += f" {SYSTEM_SEPARATOR} {detail}"
            rows.append(("class:activity.system.detail", f"{prefix} {body}"))
        remaining = len(self.queued_prompts) - visible
        if remaining > 0:
            state = "pending" if "steering" in self.queued_modes else "queued"
            rows.append(("class:plan", f"… {remaining} more {state}"))
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


@asynccontextmanager
async def suspended_editor(app: Application):
    """Hand the terminal to direct output, then repaint the editor exactly once.

    This mirrors prompt_toolkit's ``in_terminal``, except for when the editor
    is painted again. ``in_terminal`` repaints immediately after the handoff,
    before the cursor position report it has just requested arrives, so that
    paint knows nothing about the space below the cursor and lands the editor
    at its preferred height, directly under the new output. The report then
    re-renders the layout across the remaining screen ~1/30 s later (bounded
    by ``min_redraw_interval``), which moves the editor back to the bottom.
    Whenever the transcript leaves rows free below it (startup, the first tool
    calls of a session) every write makes the editor visibly jump up and back.
    Waiting for the report first paints the editor at its final position.

    To check for a regression, do not trust ``tmux capture-pane``: it shows only
    the settled frame and cannot see two paints inside one redraw interval.
    Record the raw byte stream with timestamps instead
    (``tmux pipe-pane -o 'python3 stamp.py >> out.bin'``) and look for a second
    editor paint after a scrollback write.
    """
    # Offline harnesses pass a bare stand-in for the app; nothing to suspend.
    if not isinstance(app, Application) or not app._is_running:
        yield
        return
    # Chain to any handoff already in progress, as in_terminal does.
    previous = app._running_in_terminal_f
    done: Future[None] = Future()
    app._running_in_terminal_f = done
    try:
        if previous is not None:
            await previous
        if app.output.responds_to_cpr:
            await app.renderer.wait_for_cpr_responses()
        app.renderer.erase()
        app._running_in_terminal = True
        # A popup takes over SIGWINCH, but the editor's ``_poll_output_size``
        # task keeps calling ``_on_resize`` on any size change. That erases
        # from the cursor down (over the popup) and requests a CPR the popup
        # then reads as input. Ignore resizes until the handoff repaints below.
        app._on_resize = lambda: None
        try:
            with app.input.detach(), app.input.cooked_mode():
                yield
        finally:
            del app._on_resize
            app.renderer.reset()
            app._request_absolute_cursor_position()
            try:
                # Input is attached again, so the report can be read here.
                # Rendering stays disabled meanwhile: an invalidation from a
                # keystroke or the spinner would otherwise paint the early frame.
                if app.output.responds_to_cpr:
                    await app.renderer.wait_for_cpr_responses()
            finally:
                app._running_in_terminal = False
                app._redraw()
    finally:
        if not done.done():
            done.set_result(None)


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


def editor_mode_label(app: Application) -> str:
    """Read vi state at render time, without showing a badge for Emacs editing."""
    if app.editing_mode != EditingMode.VI:
        return ""
    if app.current_buffer.selection_state:
        return " VISUAL "
    return {
        InputMode.INSERT: " INSERT ",
        InputMode.INSERT_MULTIPLE: " INSERT ",
        InputMode.NAVIGATION: " NORMAL ",
        InputMode.REPLACE: " REPLACE ",
        InputMode.REPLACE_SINGLE: " REPLACE ",
    }[app.vi_state.input_mode]


def install_reflow_renderer(app: Application) -> None:
    """Install the reflow-aware erase; only needed without resize regeneration.

    Regeneration clears the screen and scrollback before replaying, so the
    careful physical-row accounting is wasted work when it is enabled.
    """
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
        self.code_theme = code_theme or (lambda: SETTINGS["syntax_dark"].default)
        self.rich_theme = rich_theme or PALETTES["dark"].rich_theme
        self.pending: list[tuple[tuple[object, ...], str, bool]] = []
        self.transient_pending: list[tuple[tuple[object, ...], str, bool]] = []
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.commit_print = self.print
        self.commit_thinking = lambda text: self.print(
            ThinkingMarkdown(text, code_theme=self.code_theme(), style="dim"), end=""
        )
        self._thinking_tail = ""
        self._thinking_streamed = False
        self._regenerate = None
        self.resize_replay = None

    def regenerate(self, replay) -> None:
        # Coalesce requests. Snapshot only inside the handoff so arrivals during
        # the CPR await are included exactly once, including queued writes.
        self._regenerate = replay
        self.changed.set()

    def print(self, *objects, end="\n", transient=False) -> None:
        entry = (objects, end, False)
        self.pending.append(entry)
        if transient:
            self.transient_pending.append(entry)
        self.changed.set()

    def begin_turn(self, prompt: str) -> None:
        # The live row shows the running tool, not the prompt, so the quote goes
        # to scrollback as soon as the turn starts rather than waiting for output.
        self.commit_print()
        self.commit_print(TaskPrompt(prompt))
        self.commit_print()

    def end_turn(self) -> None:
        self.finish_thinking()
        self.finish()

    def _commit(self, source: str) -> None:
        if source.strip():
            self.commit_print(Markdown(source, code_theme=self.code_theme()))
            self.commit_print()

    def _commit_thinking(self, source: str) -> None:
        if source.strip():
            self.commit_thinking(source)

    def thinking_delta(self, text: str) -> None:
        """Buffer incomplete Markdown containers, just like the public answer."""
        if not text:
            return
        self._thinking_streamed = True
        for part in text.splitlines(keepends=True):
            self._thinking_tail += part
            if part.endswith("\n"):
                self._commit_blocks(thinking=True)

    def finish_thinking(self, fallback: str = "") -> None:
        if not self._thinking_streamed and fallback:
            self.thinking_delta(fallback)
        if self._thinking_streamed:
            self._commit_thinking(self._thinking_tail.rstrip("\n") + "\n\n")
        self._thinking_tail = ""
        self._thinking_streamed = False

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

    def _commit_blocks(self, *, thinking: bool = False) -> None:
        source = self._thinking_tail if thinking else self.tail
        lines = source.splitlines(keepends=True)
        tokens = Markdown(source).parsed
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
            if thinking:
                self._commit_thinking("".join(lines[:end]))
                self._thinking_tail = "".join(lines[end:])
            else:
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
            if self.pending or self._regenerate is not None:
                # Renderer.reset() shows the cursor at the transcript position
                # both when erasing and before repainting. Suppress those shows
                # until the handoff has restored the editor and its cursor.
                with self.app.output.hidden_cursor():
                    async with suspended_editor(self.app):
                        # Snapshot after entering: input/model events can arrive while
                        # the handoff waits for CPR, but not during these sync writes.
                        if self._regenerate is not None:
                            pending = self._regenerate() + self.transient_pending
                            self._regenerate = None
                            self.pending.clear()
                            # The handoff has erased the editor. Clear the
                            # normal-screen history and home before replay; its
                            # exit will request fresh CPR and restore the draft.
                            self.app.output.write_raw("\x1b[H\x1b[2J\x1b[3J")
                            self.app.output.flush()
                        else:
                            pending, self.pending = self.pending, []
                        self.transient_pending = []
                        width = max(1, self.app.output.get_size().columns)
                        # Rich's public buffer context coalesces the batch's
                        # prints (including separators) into one output flush.
                        # Keep it synchronous and inside the single-writer handoff.
                        with self.console, self.console.use_theme(self.rich_theme()):
                            for objects, end, soft_wrap in pending:
                                self.console.print(
                                    *objects, end=end, soft_wrap=soft_wrap, width=width
                                )
                # The handoff already repainted the editor on exit.
            self.changed.clear()

    async def run(self) -> None:
        size = self.app.output.get_size()
        resized_at = None
        while True:
            # Poll only when resize replay is enabled. Debounce resize storms;
            # the renderer continues handling the editor normally meanwhile.
            if self.resize_replay is None:
                await self.changed.wait()
            else:
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=0.1)
                except TimeoutError:
                    pass
                # Height changes can scroll pieces of the live preview into
                # history too; replay must clear those just like width reflow.
                current = self.app.output.get_size()
                if current != size:
                    size, resized_at = current, monotonic()
                elif resized_at is not None and monotonic() - resized_at >= 0.25:
                    self.regenerate(self.resize_replay)
                    resized_at = None
            await asyncio.sleep(1 / 30)
            await self.flush()


PROMPT_PREFIX = "❯ "
CONTINUATION_PREFIX = "· "


def create_prompt(
    registry: CommandRegistry,
    *,
    activity: Activity | None = None,
    transcript: "Transcript | None" = None,
    workspace=None,
    on_submit=None,
    on_cancel=None,
    on_effort=None,
    on_model=None,
    on_tasks=None,
    on_thinking=None,
    on_commands=None,
    on_send_mode=None,
    **kwargs,
) -> PromptSession:
    configure_newline_keys()
    activity = activity or Activity()
    keys = KeyBindings()

    @keys.add("c-o", filter=~is_searching)
    def toggle_tasks(event: KeyPressEvent) -> None:
        shown = activity.toggle_tasks()
        if on_tasks is not None:
            on_tasks(shown)
        event.app.invalidate()

    @keys.add("c-t", filter=~is_searching)
    def toggle_thinking(event: KeyPressEvent) -> None:
        activity.show_thinking = not activity.show_thinking
        if on_thinking is not None:
            on_thinking(activity.show_thinking)
        event.app.invalidate()

    if on_send_mode is not None:
        # Ctrl+S replaces forward search; Ctrl+R still opens history search.

        @keys.add("c-s", filter=~is_searching)
        def cycle_send_mode(event: KeyPressEvent) -> None:
            on_send_mode()
            event.app.invalidate()

    if on_commands is not None:
        # Ctrl+G is otherwise only an abort action; keep it native in search.
        @keys.add("c-g", filter=~is_searching)
        def toggle_command_scrollback(event: KeyPressEvent) -> None:
            on_commands()
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

        @keys.add("c-d", filter=Condition(lambda: activity.busy))
        def interrupt_turn(event):
            on_cancel()

        @keys.add("c-c")
        def interrupt(event):
            # Never discard a draft and interrupt the turn in one keypress: clear
            # the editor first, so interrupting a busy turn needs an empty prompt.
            searching = is_searching()
            if activity.busy and not searching and not session.default_buffer.text:
                on_cancel()
                return
            if searching:
                stop_search()
            session.default_buffer.reset()
            transcript.note(
                "Input discarded. Ctrl+C again interrupts."
                if activity.busy
                else "Input discarded. Ctrl+D on an empty prompt exits."
            )

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
        message=[("class:prompt", PROMPT_PREFIX)],
        prompt_continuation=lambda width, line, soft: [
            ("class:prompt", "  " if soft else CONTINUATION_PREFIX)
        ],
        # Every prefix is the same width, so wrapped rows all get the same
        # amount of text space.
        input_processors=[WordWrapProcessor(prefix_width=get_cwidth(PROMPT_PREFIX))],
        multiline=True,
        erase_when_done=True,
        completer=merge_completers([SlashCompleter(registry), FileReferenceCompleter(workspace)]),
        lexer=ReferenceLexer(),
        complete_while_typing=Condition(
            lambda: (
                (
                    get_app().current_buffer.text.startswith("/")
                    and "\n" not in get_app().current_buffer.text
                )
                or reference_fragment(get_app().current_buffer.document.text_before_cursor)
                is not None
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

    # Layout callbacks are queried repeatedly during a single synchronous redraw.
    # Never retain their results across redraws: editor/menu/CPR and mutable
    # activity state can all change without going through one revision counter.
    render_cache = None

    def per_render(function):
        @wraps(function)
        def cached(*args):
            if render_cache is None:
                return function(*args)
            key = (function, session.app.output.get_size(), args)
            if key not in render_cache:
                render_cache[key] = function(*args)
            return render_cache[key]

        return cached

    @per_render
    def frame_height() -> int:
        live = preview_layout()
        if live is not None:
            return live[2]
        size = session.app.output.get_size()
        available = max(1, size.rows - 4 - activity_height() - len(queue_rows()))
        text_height = editor.preferred_height(max(1, size.columns - 2), available).preferred
        return min(text_height, available) + 2

    plan_spinner = Spinner("arc")
    # Give the prompt line its own glyph so it reads as the overall turn, not as
    # another in-progress task row.
    prompt_spinner = Spinner("dots")
    # System rows (compaction, worktree git work) spin differently from a
    # model turn, so a wait on pcode itself is never mistaken for one on the model.
    system_spinner = Spinner("line")
    refresh_interval = (
        min(plan_spinner.interval, prompt_spinner.interval, system_spinner.interval) / 1000
    )

    @per_render
    def base_plan_rows(budget: int | None = None):
        if budget is None:
            budget = min(10, max(1, session.app.output.get_size().rows // 2 - 2))
        return activity.plan_rows(budget, plan_spinner.render(monotonic()).plain)

    @lru_cache(maxsize=1)
    def preview_body(diff: bool, body: str, width: int, theme: str):
        # Only the most recent body is retained. Titles and height/tail allocation
        # stay outside this cache; width, kind and syntax theme affect rendering.
        if diff:
            return edit_preview_rows(body, width, theme)
        return [
            ("class:bottom-toolbar.text", row.plain)
            for row in Text(command_text(body)).wrap(
                Console(width=width), width, overflow="fold", no_wrap=False
            )
        ]

    @per_render
    def preview_layout():
        """Allocate actual chrome/editor height first, then give output the remainder.

        Keep the normal task viewport unless it would leave no output at all.
        Only in that case trim task rows to preserve a one-line output tail.
        Calculate all three heights together so editor wrapping cannot create a
        circular dependency between frame_height and command_rows.
        """
        if transcript is None:
            return None
        edits = transcript.show_edits and activity.edit_previews
        # A `!command` the user typed is shown while it runs whatever the
        # scrollback setting for the model's commands says.
        commands = (
            transcript.command_scrollback or activity.user_command
        ) and activity.command_outputs
        if not edits and not commands:
            return None
        size = session.app.output.get_size()
        width = max(1, size.columns - 2)
        # One terminal row stays free for the non-full-screen renderer/CPR.
        fixed = (
            1
            + int(session.bottom_toolbar is not None)
            + status_height()
            + len(queue_rows())
            + menu.preferred_height(size.columns, size.rows).preferred
            + search.preferred_height(size.columns, size.rows).preferred
        )
        room = max(0, size.rows - fixed)
        plans = base_plan_rows()
        # Editor: two borders and at least one text row. Preview: two borders,
        # the command, and at least one output row. Keep one task when possible.
        task_floor = 3 if plans else 0
        editor_room = max(1, room - 2 - 4 - task_floor)
        editor_rows = min(editor_room, editor.preferred_height(width, editor_room).preferred)
        editor_height = editor_rows + 2
        plan_budget = max(0, room - editor_height - 4 - 2)
        if len(plans) > plan_budget:
            plans = base_plan_rows(plan_budget)
        plan_height = len(plans) + 2 if plans else 0
        budget = min(transcript.command_preview_lines, room - editor_height - plan_height - 3)
        if budget <= 0:
            return plans, [], editor_height
        # Parallel calls share the preview; show the most recently updated call.
        event = next(
            reversed((activity.edit_previews if edits else activity.command_outputs).values())
        )
        # A sandboxed snippet is pending arguments like an edit, but it is code
        # rather than a diff: no +/- coloring, and nothing has run yet.
        code = bool(edits) and event.kind == "code"
        title = (
            "Preparing code · not yet run"
            if code
            else f"Preparing edit · {event.path} · not applied"
            if edits
            else "$ " + command_preview(event.command)
        )
        body = event.text if edits else event.output
        rows = preview_body(bool(edits) and not code, body, width, transcript.code_theme)
        commands = [("class:plan", title), *rows[-budget:]]
        return plans, commands, editor_height

    def plan_rows():
        live = preview_layout()
        return live[0] if live is not None else base_plan_rows()

    def command_rows():
        live = preview_layout()
        return live[1] if live is not None else []

    @per_render
    def notice_rows():
        """Freeze the expiring notice for this render so height matches content."""
        return activity.notice_rows(session.app.output.get_size().columns)

    def status_gap() -> bool:
        """Whether the live panel needs its own blank row above it.

        Scrollback separates blocks with a blank row, but the panel is not
        scrollback: without this the spinner sits flush against the last tool
        line. Depend only on state preview_layout already reads, so asking for
        the gap cannot re-enter the layout calculation.
        """
        shown = activity.status_shown or bool(notice_rows())
        return shown and transcript is not None and not transcript.ends_blank

    def status_height() -> int:
        return activity.status_shown + len(notice_rows()) + status_gap()

    def activity_height() -> int:
        rows = plan_rows()
        commands = command_rows()
        return (
            status_height()
            + (len(rows) + 2 if rows else 0)
            + (len(commands) + 2 if commands else 0)
        )

    def plan_text():
        return panel_fragments(plan_rows(), session.app.output.get_size().columns - 2)

    current_status = ConditionalContainer(
        Window(
            FormattedTextControl(
                lambda: activity.status_fragments(
                    (prompt_spinner if activity.prompt_kind == "user" else system_spinner)
                    .render(monotonic())
                    .plain,
                    session.app.output.get_size().columns,
                ),
                show_cursor=False,
            ),
            height=1,
            wrap_lines=False,
            dont_extend_height=True,
        ),
        filter=Condition(lambda: activity.status_shown),
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
                    [
                        (
                            "class:plan.heading" if activity.displayed_plan else "bold",
                            activity.panel_heading(),
                        )
                    ],
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
    commands = ConditionalContainer(
        Frame(
            Window(
                FormattedTextControl(
                    lambda: panel_fragments(
                        command_rows(), session.app.output.get_size().columns - 2
                    ),
                    show_cursor=False,
                ),
                height=lambda: len(command_rows()),
                dont_extend_height=True,
                wrap_lines=False,
            ),
            height=lambda: len(command_rows()) + 2,
        ),
        filter=Condition(lambda: bool(command_rows())),
    )
    status_spacer = ConditionalContainer(Window(height=1), filter=Condition(status_gap))
    # Directly above the spinner: a notice answers the keystroke that caused it
    # without ever reaching scrollback, and vanishes on its own.
    notice = ConditionalContainer(
        Window(
            FormattedTextControl(
                lambda: panel_fragments(notice_rows(), session.app.output.get_size().columns),
                show_cursor=False,
            ),
            height=lambda: len(notice_rows()),
            wrap_lines=False,
            dont_extend_height=True,
        ),
        filter=Condition(lambda: bool(notice_rows())),
    )
    activity_panel = HSplit([status_spacer, commands, notice, current_status, plan])

    @per_render
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
        max_height=20, scroll_offset=1, extra_filter=has_focus(session.default_buffer)
    )
    menu.content.dont_extend_height = Always()
    editor_frame = Frame(editor, height=frame_height)
    # Replace only the bottom border: the badge must not add a row or alter CPR sizing.
    editor_frame.container.children[-1] = VSplit(
        [
            Window(char="└", width=1, style="class:frame.border"),
            Window(char="─", style="class:frame.border"),
            ConditionalContainer(
                Label(
                    lambda: editor_mode_label(session.app),
                    style="class:editor.mode",
                    dont_extend_width=True,
                ),
                filter=Condition(lambda: session.app.editing_mode == EditingMode.VI),
            ),
            Window(FormattedTextControl("─┘"), width=2, style="class:frame.border"),
        ],
        height=1,
    )
    children = [menu, search, activity_panel, queued, editor_frame]
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
            key_bindings=editor_app.key_bindings,
            editing_mode=editor_app.editing_mode,
            style=editor_app.style,
            input=editor_app.input,
            output=editor_app.output,
            mouse_support=False,
        )
    animation_task = None

    def needs_animation():
        return (
            activity.busy
            or activity.status_shown
            # Keep redrawing while a notice is live: nothing else will ask for
            # the frame that finally removes it.
            or activity.notice_shown
            or (activity.tasks_shown and bool(activity.tools.calls))
        )

    async def animate(app):
        nonlocal animation_task
        await asyncio.sleep(refresh_interval)
        animation_task = None
        # Repaint unconditionally: this timer only exists because the previous
        # render was animated, and the frame that removes an expired notice or
        # a finished spinner is the one nothing else asks for.
        app.invalidate()

    def before_render(app):
        nonlocal render_cache
        render_cache = {}
        if transcript is None or not (
            (transcript.show_edits and activity.edit_previews)
            or (transcript.command_scrollback and activity.command_outputs)
        ):
            preview_body.cache_clear()

    def after_render(app):
        nonlocal render_cache, animation_task
        render_cache = None
        # A redraw caused by input or application events starts animation again.
        # Idle prompts have no timer; toolkit owns cancellation at app shutdown.
        if needs_animation() and app.is_running:
            if animation_task is None or animation_task.done():
                animation_task = app.create_background_task(animate(app))
        elif animation_task is not None:
            animation_task.cancel()
            animation_task = None

    session.app.before_render += before_render
    session.app.after_render += after_render
    if session.app.editing_mode == EditingMode.VI:
        # Allow terminal escape sequences to arrive, without a half-second pause.
        session.app.ttimeoutlen = 0.1
    if transcript is None or not transcript.replays_on_resize:
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
        preferences: dict[str, str] | None = None,
        detected_theme: str | None = None,
    ) -> None:
        preferences = load_preferences() if preferences is None else preferences
        self.error_scrollback_lines = int(preferences.get("error_scrollback_lines", "20"))
        self.tool_error_scrollback = preferences.get("tool_error_scrollback", "off") == "on"
        self.show_edits = preferences.get("show_edits", "on") == "on"
        self.command_scrollback = preferences.get("show_commands", "off") == "on"
        self.command_scrollback_lines = int(preferences.get("command_scrollback_lines", "20"))
        self.command_preview_lines = int(preferences.get("command_preview_lines", "10"))
        self.activity = activity
        self.console = console
        self.theme = theme
        self.detected_theme = detect_theme() if detected_theme is None else detected_theme
        self.color_style = color_style
        self.syntax_themes = syntax_themes(preferences)
        self._output: TerminalOutput | None = None
        self.regenerate_on_resize = preferences.get("regenerate_on_resize", "on") == "on"
        self.log = TranscriptLog()
        self._replay_sink: list | None = None
        self._block: str | None = None

    @property
    def replays_on_resize(self) -> bool:
        """Whether a width change rebuilds scrollback instead of reflowing in place."""
        return self.regenerate_on_resize and self.console.is_terminal

    @property
    def output(self) -> TerminalOutput | None:
        return self._output

    @output.setter
    def output(self, output: TerminalOutput | None) -> None:
        self._output = output
        if output is not None:
            output.commit_print = self.print
            output.commit_thinking = self.thinking
            if self.replays_on_resize:
                output.resize_replay = self.replay

    @recorded
    def print(self, *objects, end="\n", tool_line: bool = False) -> None:
        """Write scrollback, keeping tool lines one block apart from other output."""
        # Resolve theme-dependent renderables again on every replay.
        objects = tuple(
            Markdown(obj.markup, code_theme=self.code_theme)
            if isinstance(obj, (Markdown, RetainedMarkdown))
            else replace(obj, code_theme=self.code_theme)
            if isinstance(obj, (TranscriptNotice, CommandTranscript, EditTranscript))
            else obj
            for obj in objects
        )
        block = "tools" if tool_line else "blank" if self._ends_blank(objects) else "other"
        # Consecutive tool lines stay flush; entering or leaving that run gets a
        # blank row, unless the preceding write already ended with one.
        if self._block not in (None, "blank", block) and "tools" in (self._block, block):
            self._write((), "\n")
        self._block = block
        self._write(objects, end)

    @property
    def ends_blank(self) -> bool:
        """Whether scrollback already ends with a blank row.

        The live activity panel uses this to keep itself one block apart from
        the last thing written, the same way consecutive writes do.
        """
        return self._block in (None, "blank")

    @staticmethod
    def _ends_blank(objects: tuple) -> bool:
        """Report whether this write already leaves a blank row behind it.

        A bare ``print()`` is the usual separator; thinking is the one
        renderable here that pads itself, so only it needs naming.
        """
        return not objects or isinstance(objects[-1], ThinkingMarkdown)

    def _write(self, objects: tuple, end: str) -> None:
        if self._replay_sink is not None:
            self._replay_sink.append((objects, end, False))
        elif self.output is not None:
            self.output.print(*objects, end=end)
        else:
            with self.console.use_theme(self.rich_theme):
                self.console.print(*objects, end=end)

    @recorded
    def thinking(self, text: str) -> None:
        """Retain readable provider text, choosing visibility again on every redraw."""
        if self.activity is not None and self.activity.show_thinking:
            self.print(ThinkingMarkdown(command_text(text), code_theme=self.code_theme), end="")

    @recorded
    def tool_result(self, event: ToolSummary) -> None:
        """Retain hidden results too; choose one representation on each replay."""
        if self.writes_tool_result(event):
            self.events((event,))

    @recorded
    def edit(self, event) -> None:
        if self.show_edits:
            self.print(EditTranscript(event, code_theme=self.code_theme))

    def replay(self) -> list:
        """Project the retained log with current settings, without recording again."""
        sink = []
        self._replay_sink = sink
        self._block = None
        self.log.recording = False
        try:
            if self.log.dropped:
                self.note("Earlier transcript entries omitted from this regenerated view.")
            for entry in self.log.entries:
                getattr(self, entry.method)(*entry.args, **entry.kwargs)
        finally:
            self.log.recording = True
            self._replay_sink = None
        return sink

    def regenerate(self) -> None:
        """Request an atomic rebuild; never emit terminal escapes into redirected output."""
        if self.output is not None and self.console.is_terminal:
            self.output.regenerate(self.replay)

    def clear(self) -> None:
        """Drop retained scrollback and rebuild the screen from what comes next.

        The rebuild is deferred to the next flush, so writes made after this
        call are replayed onto the cleared screen rather than erased with it.
        """
        self.log.clear()
        self.regenerate()

    @property
    def resolved_theme(self) -> str:
        return self.detected_theme if self.theme == "auto" else self.theme

    @property
    def palette(self) -> Palette:
        return PALETTES[self.resolved_theme]

    @property
    def menu_palette(self) -> Palette:
        """The palette implied by the syntax style in use, for the popup.

        `/colors terminal` has no Pygments style to read -- code falls back to
        the ANSI pseudo-styles -- so the hardcoded palette stands in.
        """
        if self.color_style == "terminal":
            return self.palette
        return syntax_palette(self.syntax_themes[self.resolved_theme], self.palette)

    @property
    def chrome_palette(self) -> Palette:
        """The same style read for text drawn straight onto the terminal.

        The popup brings its own background; the prompt, the plan rows and the
        frame do not, so the palette's surface stands in for the terminal's
        background and any color that would be lost against it is dropped.
        """
        if self.color_style == "terminal":
            return self.palette
        style = self.syntax_themes[self.resolved_theme]
        return syntax_palette(style, self.palette, self.palette.surface)

    def prompt_style(self) -> Style:
        """The prompt_toolkit style for the current theme and syntax style."""
        return self.chrome_palette.prompt_style(self.menu_palette)

    @property
    def rich_theme(self) -> Theme:
        return TERMINAL_THEME if self.color_style == "terminal" else self.palette.rich_theme()

    @property
    def code_theme(self) -> str:
        return (
            f"ansi_{self.resolved_theme}"
            if self.color_style == "terminal"
            else self.syntax_themes[self.resolved_theme]
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
            self.retained_note(f"Coder · workspace: {workspace}")
            self.retained_note("Live model · file edits and shell tools enabled · not a sandbox")
        else:
            self.retained_note("Local only · no model connected · no files or shell tools")
        self.retained_note(
            "Type / for commands, /theme-preview for a sample response, /help for keys."
        )
        self.print()

    @recorded
    def syntax_gallery(self) -> None:
        """Preview every selectable style, marking the one in use.

        Recorded without arguments so a replay re-reads the current palette and
        saved styles: the marked row still answers "what am I looking at?"
        after `/theme` or `/syntax` rebuilds scrollback.
        """
        self.print(
            SyntaxGallery(
                SYNTAX_THEMES,
                palette=self.resolved_theme,
                dark=self.syntax_themes["dark"],
                light=self.syntax_themes["light"],
                terminal_colors=self.color_style == "terminal",
            )
        )

    @recorded
    def retained_note(self, text: str) -> None:
        """Show a notice that belongs to scrollback, so a redraw keeps it.

        Most notices answer a keystroke and are dismissed by the next redraw.
        The opening banner and what it reports about this session are history,
        not an answer, so a resize must not wipe them.
        """
        self.print(Text(text, style="pcode.muted"))

    def flash(self, text: str) -> None:
        """Answer a keystroke in the live panel instead of in scrollback.

        Acknowledgements ("Show thinking: off") are worth a glance and nothing
        more; writing them to scrollback leaves them between the model's output
        forever. Without a live panel there is nowhere to put one, so fall back
        to a plain notice.
        """
        if self.activity is None or self.output is None:
            self.note(text)
            return
        self.activity.flash(text)
        self.output.app.invalidate()

    def note(self, text: str) -> None:
        """Show an informational notice once, without retaining it for redraws."""
        notice = Text(text, style="pcode.muted")
        if self._replay_sink is not None:
            self._replay_sink.append(((notice,), "\n", False))
        elif self.output is not None:
            # Preserve new notices across an already queued redraw, but never
            # replay notices that have previously reached the terminal.
            self.output.print(notice, transient=True)
        else:
            with self.console.use_theme(self.rich_theme):
                self.console.print(notice)

    @recorded
    def error(self, text: str, *, title: str = "Error") -> None:
        self.print(
            TranscriptNotice(text, "error", title, self.error_scrollback_lines, self.code_theme)
        )

    def streams_command(self, event: Event) -> bool:
        """Report whether this settled tool will be mirrored into scrollback.

        Captured output is the bulky part, so mirroring a failed command also
        needs the failure option; without it the completion still shows, as the
        summary line a successful call would leave.
        """
        if not isinstance(event, ToolSummary) or event.name not in COMMAND_TOOLS:
            return False
        return self.command_scrollback and (self.tool_error_scrollback or not event.failed)

    def writes_tool_result(self, event: Event) -> bool:
        """Report whether this settled tool reaches scrollback at all.

        Every call the live panel drops is written here instead, except where
        something else already tells the story: the task panel owns successful
        planning calls, and a shown diff owns successful edits. Neither tells
        the story of a failure, so a failed call is always written; only how
        much of it, its summary line or its diagnostic, is configurable.
        """
        if not isinstance(event, ToolSummary):
            return False
        # One option governs every command completion, success or failure.
        if event.name in COMMAND_TOOLS:
            return self.command_scrollback
        if event.failed:
            return True
        return event.name not in PLAN_TOOLS and not (event.name in EDIT_TOOLS and self.show_edits)

    @recorded
    def command_output(self, event: ToolSummary) -> bool:
        """Mirror a command and its captured output; report whether anything printed."""
        if not self.streams_command(event):
            return False
        invocation = (
            command_text(event.command) if event.command else plain(event.detail, limit=None)
        )
        # Live results arrive redacted and length-bounded from the capture step;
        # sanitize again so replayed or synthesized events cannot emit controls.
        output = command_text(event.result or event.error or "").rstrip("\n")
        if not output.strip():
            output = "(no output)"
        self.print(
            CommandTranscript(
                command=invocation,
                output=output,
                title=label(event.name),
                failed=event.failed,
                elapsed_seconds=event.elapsed_seconds,
                max_lines=self.command_scrollback_lines,
                code_theme=self.code_theme,
                shell_command=bool(event.command),
            )
        )
        return True

    @recorded
    def shell_result(
        self, command: str, output: str, *, failed: bool, elapsed_seconds: float | None
    ) -> None:
        """Mirror a `!command` the user ran; always shown, since they asked for it."""
        output = command_text(output).rstrip("\n")
        self.print(
            CommandTranscript(
                command=command_text(command),
                output=output if output.strip() else "(no output)",
                title="Shell",
                failed=failed,
                elapsed_seconds=elapsed_seconds,
                max_lines=self.command_scrollback_lines,
                code_theme=self.code_theme,
            )
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
        # Scrollback shows the outcome only: the live panel already named the
        # target while the call ran. The session browser, which has no such
        # panel, passes the whole detail to the same renderer.
        result = (
            " · " + plain(event.detail.rsplit(" → ", 1)[-1], limit=60)
            if event.failed or (event.name != "run_command" and " → " in event.detail)
            else ""
        )
        for line in tool_summary_lines(
            event.name,
            result,
            failed=event.failed,
            elapsed_seconds=event.elapsed_seconds,
            command=event.command,
            width=self.console.width,
        ):
            self.print(line, tool_line=True)

    @recorded
    def events(self, events: tuple[Event, ...], *, show_tools: bool = False) -> None:
        for event in events:
            if isinstance(event, CacheBust):
                self.print(
                    TranscriptNotice(command_text(event.text), "warning", "Prompt cache miss")
                )
            elif isinstance(event, Thinking):
                self.thinking(event.text.rstrip("\n") + "\n\n")
            elif isinstance(event, Message):
                self.print(Markdown(event.markdown, code_theme=self.code_theme))
                self.print()
            elif isinstance(event, ToolSummary):
                if event.name in COMMAND_TOOLS:
                    # Mirroring owns command completions. A failure whose
                    # captured output is withheld still reports the call.
                    if self.command_scrollback and not self.command_output(event):
                        self.command_summary(event)
                    continue
                if event.failed and self.tool_error_scrollback:
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
                        (f"{'✗' if event.failed else '✓'} {label(event.name)}  ", "pcode.thinking"),
                        (plain(event.detail, limit=None), "pcode.thinking"),
                        (
                            f"  {event.elapsed_seconds:.1f}s"
                            if event.elapsed_seconds is not None
                            else "",
                            "pcode.thinking",
                        ),
                    ),
                    tool_line=True,
                )

    def help(self, registry: CommandRegistry) -> None:
        table = Table(box=None, padding=(0, 2), show_header=False)
        table.add_column(style="pcode.accent", no_wrap=True)
        table.add_column()
        for group, commands in registry.grouped():
            table.add_row(Text(group, style="pcode.muted"), "")
            for command in commands:
                name = " ".join((command.name, *command.aliases))
                table.add_row(name, command.description)
        self.print(table)
        self.print()
        self.note("Enter send · Alt+Enter newline (or Esc, Enter) · Tab/↑/↓ complete")
        self.note("Enter accepts a selected completion; press again to send.")
        self.note("Ctrl+O tasks widget · Ctrl+T thinking · Ctrl+G command output (each redraws)")
        self.note("Ctrl+L choose model · Ctrl+N raise effort · Ctrl+P lower effort (next turn)")
        self.note("Ctrl+R search history · Ctrl+C discard input · Ctrl+D exit on empty input")
        self.note(
            "During a run: Enter sends · Ctrl+S cycles steering/queue/interrupt. "
            "Ctrl+C discards a draft first, then cancels · Ctrl+D cancels, keeps draft."
        )
        self.note("Cancellation clears queued messages. Use terminal/tmux scrollback for history.")
        self.print()
