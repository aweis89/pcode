"""Editable prompt with replayable output in the terminal's normal scrollback."""

import asyncio
import os
import re
from asyncio import Future
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import asdict, dataclass, field, replace
from functools import cache, cached_property, lru_cache, partial, wraps
from io import StringIO
from time import monotonic

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app, get_app_or_none
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import merge_completers
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Always, Condition, has_focus
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import VerticalAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.renderer import Renderer
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame, Label
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from pcode.block import INDENT, RULE, RUNNING, block_heading
from pcode.command_transcript import CommandTranscript
from pcode.commands import CommandRegistry, SlashCompleter
from pcode.edit_transcript import EditTranscript, edit_preview_rows
from pcode.file_refs import FileReferenceCompleter, ReferenceLexer, reference_fragment
from pcode.input_keys import configure_newline_keys
from pcode.jobs import WATCHED_PREFIX
from pcode.layout_speed import install_fast_layout_division
from pcode.paste import MARKER_PATTERN
from pcode.preferences import (
    SETTINGS,
    SYNTAX_THEMES,
    TERMINAL_SYNTAX,
    load_preferences,
)
from pcode.prefix_keys import PrefixKeys, shortcut_label
from pcode.prompt_keys import PromptCallbacks, prompt_key_bindings
from pcode.runtime import CacheBust, CommandOutput, Event, Message, Thinking, ToolSummary
from pcode.shell_mode import SHELL_PREFIX
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
    split_outcome,
    tool_summary_lines,
)
from pcode.tool_panel import DELEGATE, TASK_ROWS, ToolHistory, panel_fragments, task_panel_rows
from pcode.transcript_log import RetainedMarkdown, TranscriptLog, recorded
from pcode.transcript_notice import TranscriptNotice
from pcode.word_wrap import WordWrapProcessor
from pcode.workers import Workers


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
        highlight = (
            f"reverse bg:default {menu.accent}"
            if menu.selected == "reverse"
            else f"noreverse bg:{menu.selected} {menu.accent}"
        )
        return Style.from_dict(
            {
                "plan": self.muted,
                "plan.heading": f"nodim {self.task_heading} bold",
                "plan.active": f"nodim {self.accent} bold",
                # A running sub-agent's row: its own shade, so it never reads
                # as one of the tasks it sits among.
                "plan.agent": f"nodim {self.task_heading}",
                "prompt": f"{self.accent} bold",
                # The live area has three weights. Live: the spinner and the
                # phase word, the one thing that says the turn is moving.
                # Content: what it is doing, in the terminal's own text colour.
                # Chrome: counts, clocks, notices, jobs and queues, muted.
                "activity.spinner": self.accent,
                "activity.badge": self.accent,
                "activity.phase": f"nodim {self.accent} bold",
                "activity.detail": "nodim",
                "activity.meta": self.muted,
                # The thinking row under the status row: the model's newest
                # thought, faded so it never competes with the live phase.
                "activity.thinking": f"italic {self.muted}",
                # System work is pcode's own: the badge and accent mark it, and
                # its queued rows keep an italic detail.
                "activity.system": self.accent,
                "activity.system.detail": f"italic {self.muted}",
                # Short-lived answers to a keystroke live above the spinner
                # rather than in scrollback; italics mark them as chrome.
                "activity.notice": f"italic {self.muted}",
                # Running background jobs are chrome like the spinner row.
                "activity.job": self.muted,
                # A run's pending group line, shown only when no status row
                # carries its tally. Still live, so muted rather than dimmed
                # like the settled scrollback line it becomes.
                "activity.group": self.muted,
                # A side question runs beside the turn, not as part of it, so its
                # row spins in the muted shade rather than the accent.
                "activity.aside": self.muted,
                # The live preview block, drawn the way scrollback draws a
                # settled one: a heading on the opening line, rules around it.
                "block.rule": self.muted,
                "block.heading": self.accent,
                "frame.border": self.muted,
                "editor.mode": "noreverse nodim bg:#b8b8b8 fg:#ffffff",
                # Keep foreground and background paired with the terminal theme:
                # the app palette may still be dark on a light terminal.
                "bottom-toolbar": "noreverse nodim bg:default fg:default",
                "bottom-toolbar.text": "fg:default",
                # Same roles as the prompt chrome: what matters (where, what is
                # running) in the accent, labels muted so the row stays quiet.
                "bottom-toolbar.sep": self.muted,
                "bottom-toolbar.location": f"{self.accent} bold",
                "bottom-toolbar.mode": self.task_heading,
                "bottom-toolbar.model": self.accent,
                "bottom-toolbar.context": self.muted,
                "bottom-toolbar.context-value": self.accent,
                "bottom-toolbar.activity": f"{self.task_heading} bold",
                "bottom-toolbar.cache": self.muted,
                "completion-menu": f"bg:{menu.surface} {menu.foreground}",
                "completion-menu.completion": f"bg:{menu.surface} {menu.foreground}",
                # The toolkit's selected-row default uses reverse; explicitly
                # disable it so light themes keep dark text on a light surface.
                "completion-menu.completion.current": f"{highlight} bold",
                "completion-menu scrollbar.background": f"bg:{menu.surface}",
                "completion-menu scrollbar.button": (
                    "reverse bg:default" if menu.selected == "reverse" else f"bg:{menu.selected}"
                ),
                # Full-screen popups keep native body surfaces, but share the
                # completion menu's paired highlight colors, even when a syntax
                # style has a different appearance from the terminal.
                "popup selected": f"{highlight} nodim nounderline",
                "popup cursor-line": f"{highlight} nodim nounderline",
                "popup scrollbar.background": f"noreverse bg:{menu.surface} fg:default",
                "popup scrollbar.button": f"noreverse bg:{menu.accent} fg:default",
                "popup scrollbar.arrow": f"noreverse bg:default {self.accent} bold",
                "completion-menu.meta.completion": f"bg:{menu.surface} {menu.muted}",
                "completion-menu.meta.completion.current": (
                    "reverse bg:default fg:default"
                    if menu.selected == "reverse"
                    else f"bg:{menu.selected} {menu.foreground}"
                ),
                # A file reference is neither prose nor a command: underlining
                # it marks the token without competing with the prompt chevron.
                "reference": f"{self.task_heading} underline",
                # The marker stands in for hidden text; make it impossible to
                # mistake for something the user typed.
                "paste-marker": "bold reverse",
                "auto-suggestion": self.muted,
            }
        )


PALETTES = {
    "dark": Palette("#88c0d0", "#8994a6", "#242933", "#e5e9f0", "#384457", "#c4b5fd"),
    "light": Palette("#006b80", "#586575", "#edf0f4", "#202630", "#d0e7ef", "#7c3aed"),
}

# `/syntax terminal`: named ANSI colors, so the prompt, plan rows and popup
# follow the terminal's own scheme. Muted text is `dim` rather than bright
# black, which several schemes (Solarized) paint as the background itself, and
# the selected row is reversed for the same reason. `fg:default` keeps the
# toolkit's own RGB defaults (black popup metadata, grey suggestions) from
# showing through.
TERMINAL_PALETTE = Palette(
    "ansicyan", "fg:default dim", "default", "default", "reverse", "ansimagenta"
)


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
# Scrollback columns a sub-agent's calls sit in from their delegate's row.
CHILD_INDENT = 4
NOTICE_ROWS = 6
# Background jobs get a few rows, never the screen; `/jobs` has the full list.
JOB_ROWS = 3
# Running side questions likewise; `/btw` has the full list.
ASIDE_ROWS = 3
# A wait on the session host shorter than this never gets a row: most answer
# within a frame or two, and a row that flashes for them reads as a glitch.
WAIT_GRACE_SECONDS = 0.25


# The status row is redrawn several times a second while a turn runs, so a
# longer gap means the row went away; its phase clock starts over.
PHASE_GAP_SECONDS = 1.0
# Status text the row leads with verbatim; anything longer is detail.
PHASE_WORDS = 3
# Cells of detail worth more than the status row's tally and clock.
DETAIL_MIN_CELLS = 16
# Characters of streamed thinking kept for the status row: its latest line.
THINKING_KEEP = 2000


def status_parts(status: str) -> tuple[str, str]:
    """Split free-text status into the row's phase word and its detail.

    Several modules write `Activity.status`; the convention is `Phase · detail…`
    (`Running shell · src…`, `Retrying · Overloaded…`). A short bare status
    is all phase (`Thinking…`); a sentence with no phase becomes detail, so it
    never renders as one long highlighted word.
    """
    text = plain(status, limit=None).strip().removesuffix("…").strip()
    phase, separator, detail = text.partition(" · ")
    if separator:
        return phase, detail.removesuffix("…").strip()
    if not text:
        return "Working", ""
    if len(text.split()) <= PHASE_WORDS:
        return text, ""
    return "Working", text


def clock(seconds: float) -> str:
    """`8s`, `2m05s`: whole seconds, since the row is not a stopwatch."""
    seconds = max(0, int(seconds))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m{seconds % 60:02d}s"


@dataclass
class StatusLine:
    """The status row's parts, in the one order every state uses.

    Left: spinner, optional badge, the phase (the live word, accented), then
    the detail as ordinary text. Right, in a fixed column: the run's tool tally
    and the phase clock, muted. Narrow panes drop the tally, then the clock,
    then cut the detail, and only then the phase.
    """

    phase: str
    detail: str = ""
    tally: str = ""
    elapsed: float | None = None
    badge: str = ""
    separator: str = "·"
    # A just-finished call held on the row: its detail is muted, not live.
    settled: bool = False

    def fragments(self, spinner: str, width: int) -> list[tuple[str, str]]:
        if width < 1:
            return []
        head = [("class:activity.spinner", f"{spinner} ")]
        if self.badge:
            head.append(("class:activity.badge", f"{self.badge} "))
        head.append(("class:activity.phase", self.phase))
        needed = sum(cell_len(text) for _, text in head)
        detail = f" {self.separator} {plain(self.detail, limit=None)}" if self.detail else ""
        # The meta column gives way before the detail is cut to a stub.
        wanted = needed + min(cell_len(detail), DETAIL_MIN_CELLS)
        meta = [part for part in (self.tally, self._clock()) if part]
        while meta and wanted + 2 + cell_len(" · ".join(meta)) > width:
            meta.pop(0)
        suffix = " · ".join(meta)
        room = width - (cell_len(suffix) + 2 if suffix else 0)
        # A detail with no room to say anything is dropped, not left as `·…`.
        if detail and room - needed >= DETAIL_MIN_CELLS // 2:
            style = "class:activity.meta" if self.settled else "class:activity.detail"
            head.append((style, detail))
        fitted = fit_fragments(head, room)
        if not suffix:
            return fitted
        pad = room - sum(cell_len(text) for _, text in fitted) + 2
        return [*fitted, ("", " " * pad), ("class:activity.meta", suffix)]

    def _clock(self) -> str:
        return "" if self.elapsed is None else clock(self.elapsed)


def tail_cells(text: str, width: int) -> str:
    """The last `width` cells of `text`, marking a cut with a leading ellipsis."""
    if cell_len(text) <= width:
        return text
    if width < 1:
        return ""
    kept: list[str] = []
    cells = 1  # The ellipsis.
    for char in reversed(text):
        cells += cell_len(char)
        if cells > width:
            break
        kept.append(char)
    return "…" + "".join(reversed(kept)).lstrip()


# A summary section's title: `**Tracing the resize path**` or `## Tracing...`.
# One bold run only: `**A** and **B**` is prose with emphasis, not a title.
THOUGHT_HEADING = re.compile(r"\*\*(?P<bold>[^*]+)\*\*|#{1,6}\s+(?P<hash>.+)")


def latest_thought(text: str) -> str:
    """What the thinking row says for one block of streamed thinking.

    Detailed summaries (OpenAI's, and many of Anthropic's) come in titled
    sections; the newest title reads at a glance where the prose under it
    would scroll past. Untitled text (Anthropic's summaries, progress
    updates) shows its newest line.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title, fenced = "", False
    for line in lines:
        if line.startswith("```"):
            fenced = not fenced  # A `# comment` in a code block is not a title.
        elif not fenced and (heading := THOUGHT_HEADING.fullmatch(line)):
            title = (heading["bold"] or heading["hash"]).strip()
    if title:
        return title
    # The buffer keeps the block's tail only (THINKING_KEEP), so a long
    # section can outlive its title; its newest line stands in then.
    return lines[-1].replace("**", "") if lines else ""


def fit_fragments(fragments: list[tuple[str, str]], width: int) -> list[tuple[str, str]]:
    """Cut styled fragments to `width` cells, marking a cut with an ellipsis.

    Newlines and control characters are flattened first, so a pasted command
    cannot break the row. The ellipsis goes on the last fragment kept; a
    spinner or badge that cannot fit whole is cropped without one.
    """
    fragments = [(style, plain(text, limit=None)) for style, text in fragments]
    if sum(cell_len(text) for _, text in fragments) <= width:
        return fragments
    fitted = []
    remaining = max(0, width)
    for style, text in fragments:
        # Strictly less: the fragment that overflows needs a cell for the ellipsis.
        if cell_len(text) < remaining:
            fitted.append((style, text))
            remaining -= cell_len(text)
            continue
        part = Text(text)
        part.truncate(remaining, overflow="ellipsis" if remaining > 1 else "crop")
        if part.plain:
            fitted.append((style, part.plain))
        break
    return fitted


@dataclass(eq=False)
class Wait:
    """Something this terminal is waiting on, and since when."""

    label: str
    started: float = field(default_factory=monotonic)


def _invalidate() -> None:
    """Repaint the running editor, if there is one; state can change outside it."""
    if (app := get_app_or_none()) is not None:
        app.invalidate()


def chrome_rows(text: str, width: int, style: str) -> list[tuple[str, str]]:
    """Wrap transient chrome text to the pane, bounded so it cannot take the screen."""
    if width < 1:
        return []
    console = Console(width=width)
    rows = [
        (style, row.plain)
        for line in text.splitlines()
        for row in Text(plain(line, limit=None)).wrap(
            console, width, overflow="fold", no_wrap=False
        )
    ]
    return rows[:NOTICE_ROWS]


@dataclass
class Activity:
    show_tasks: bool = True
    # Hide the widget again as soon as a turn ends, without forgetting that the
    # user wants it shown while the model works.
    autohide_tasks: bool = False
    # Draw the widget as the top section of the editor box instead of its own box.
    attach_tasks: bool = True
    # Cap on the task widget plus the editor box: whole rows, or a share of the
    # screen below 1 (0.5 is half). None keeps the default layout.
    tasks_max_height: float | None = None
    tasks_autohidden: bool = False
    # Where the model's thinking shows: `off`, `status-line` (its own row
    # under the status row), or `scrollback`. See THINKING_MODES.
    thinking_mode: str = "status-line"
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
    workers: Workers = field(default_factory=Workers)
    command_outputs: dict[str, CommandOutput] = field(default_factory=dict)
    edit_previews: dict = field(default_factory=dict)
    notice: str = ""
    notice_expires: float = 0.0
    # The footer's note of this turn's latest prompt-cache drop, e.g.
    # `cache miss 0/166k`; the full notice is only in the session journal.
    cache_note: str = ""
    # Shell jobs nothing on screen accounts for: running with no tool call
    # waiting on them, or finished before the terminal could say so. Rows are
    # rendered once a second by the app's job watcher, not per frame.
    jobs: list[tuple[str, str]] = field(default_factory=list)
    # A job whose output tail is pinned into the command preview by `/jobs watch`.
    watched_job: str = ""
    # The session's side-question records (`Asides.items`, shared, not copied):
    # the running ones get a spinner row below the prompt's.
    asides: list = field(default_factory=list)
    # What this terminal is waiting on (the session host starting, a command
    # it has not finished): its own state, never synced from the host.
    waits: list[Wait] = field(default_factory=list)
    # The tail of the newest thinking block, for the thinking row. Derived
    # from the events this terminal renders, never synced from the host.
    thought: str = ""
    # No thinking block is open: the next delta (or whole block) starts one.
    thought_done: bool = True
    # The status row's phase clock: (phase, since, last drawn). Drawing state,
    # so it is never compared, copied into a repr, or synced from the host.
    _phase: tuple[str, float, float] = field(
        default=("", 0.0, 0.0), init=False, repr=False, compare=False
    )

    @property
    def show_thinking(self) -> bool:
        """Whether scrollback carries the model's thinking."""
        return self.thinking_mode == "scrollback"

    def think(self, text: str) -> None:
        """Add streamed thinking; a new block replaces the last one."""
        if not text:
            return
        if self.thought_done:
            self.thought, self.thought_done = "", False
        self.thought = (self.thought + text)[-THINKING_KEEP:]

    def thought_fragments(self, width: int) -> list[tuple[str, str]]:
        """The thinking row under the status row, in `status-line` mode.

        Kept for the rest of the turn once a thought arrives: the last one
        usually explains the tool calls that follow it, and a row that came
        and went with every block would make the editor jump.
        """
        if self.thinking_mode != "status-line" or not self.status_shown or width < 3:
            return []
        thought = latest_thought(self.thought)
        if not thought:
            return []
        # Indented past the spinner, so it reads as the phase's own detail.
        return [
            ("class:activity.thinking", "  " + tail_cells(plain(thought, limit=None), width - 2))
        ]

    def begin_wait(self, label: str) -> Wait:
        """Start a wait that shows a spinner row once it outlasts the grace period."""
        wait = Wait(label)
        self.waits.append(wait)
        _invalidate()  # Starts the animation timer that draws the row later.
        return wait

    def end_wait(self, wait: Wait) -> None:
        if wait in self.waits:
            self.waits.remove(wait)
            _invalidate()

    @contextmanager
    def waiting(self, label: str):
        wait = self.begin_wait(label)
        try:
            yield wait
        finally:
            self.end_wait(wait)

    def wait_fragments(self, spinner: str, width: int):
        """The newest wait past its grace period, as a system row; empty otherwise.

        A running status row already says what the host is doing (a turn, or a
        command's `◈ label ▸ detail` job), so the terminal's own wait on that
        same work stays out of the way: one spinner at a time.
        """
        if self.status_shown:
            return []
        now = monotonic()
        shown = [wait for wait in self.waits if now - wait.started >= WAIT_GRACE_SECONDS]
        if not shown or width < 1:
            return []
        wait = shown[-1]
        text = Text(f"{spinner} {SYSTEM_BADGE} {plain(wait.label, limit=None)}")
        text.append(f" \u00b7 {now - shown[0].started:.0f}s")
        text.truncate(width, overflow="ellipsis")
        return [("class:activity.system", text.plain)]

    @property
    def asides_running(self) -> bool:
        return any(aside.running for aside in self.asides)

    def aside_rows(self, spinner: str, width: int, budget: int = ASIDE_ROWS):
        """One muted spinner row per running side question, folded to the budget."""
        running = [aside for aside in self.asides if aside.running]
        if budget <= 0 or not running or width < 1:
            return []
        shown = running if len(running) <= budget else running[: max(0, budget - 1)]
        rows = []
        for aside in shown:
            parts = [f"{spinner} btw", plain(aside.question, limit=None)]
            if aside.label:
                parts.insert(1, aside.label)
            if aside.activity:
                parts.append(plain(aside.activity, limit=None))
            parts.append(f"{aside.elapsed:.0f}s")
            text = Text(" \u00b7 ".join(parts))
            text.truncate(width, overflow="ellipsis")
            rows.append(("class:activity.aside", text.plain))
        if len(shown) < len(running):
            rows.append(("class:activity.aside", f"\u2026 {len(running) - len(shown)} more (/btw)"))
        return rows

    def job_rows(self, budget: int) -> list[tuple[str, str]]:
        """The jobs row block, folded to the budget so it never crowds the editor."""
        if budget <= 0 or not self.jobs:
            return []
        if len(self.jobs) <= budget:
            return list(self.jobs)
        shown = self.jobs[: max(0, budget - 1)]
        remaining = len(self.jobs) - len(shown)
        return shown + [("class:activity.job", f"\u2026 {remaining} more jobs (/jobs)")]

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
        if not self.notice_shown:
            return []
        return chrome_rows(self.notice, width, "class:activity.notice")

    def panel_heading(self) -> str:
        return self.panel_title()

    def reset(self) -> None:
        """Clear the panel for a new conversation, keeping the draft and queue."""
        self.command_outputs.clear()
        self.edit_previews.clear()
        self.plan = []
        self.plan_preview = None
        self.tools.clear()
        self.workers = Workers()
        self.prompt = ""
        self.prompt_state = ""
        self.prompt_kind = "user"
        self.prompt_detail = ""
        self.status = ""
        self.thought, self.thought_done = "", True
        self.tasks_autohidden = False

    def height_cap(self, rows: int) -> int | None:
        """The task widget plus editor box's row limit on a screen this tall."""
        cap = self.tasks_max_height
        if cap is None:
            return None
        return min(rows, int(rows * cap) if cap < 1 else int(cap))

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
        self.thought, self.thought_done = "", True
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
        # A configured height is room the user asked the tasks to fill.
        max_tasks = TASK_ROWS if self.tasks_max_height is None else budget
        return task_panel_rows(self.displayed_plan, self.tools, budget, icon, max_tasks)

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

    def _phase_seconds(self, phase: str) -> float:
        """Seconds the row has shown this phase: a stall reads as `Thinking · 40s`.

        Kept by whoever draws the row rather than synced from the host, and
        restarted after a gap in drawing, which only happens between turns.
        """
        now = monotonic()
        shown, since, seen = self._phase
        if shown != phase or now - seen > PHASE_GAP_SECONDS:
            since = now
        self._phase = (phase, since, now)
        return now - since

    def status_line(self, tally: str = "") -> StatusLine:
        """What the status row says, before it is fitted to a width."""
        if self.prompt_kind != "user":
            phase = plain(self.prompt, limit=None)
            return StatusLine(
                phase,
                plain(self.prompt_detail, limit=None),
                badge=SYSTEM_BADGE,
                separator=SYSTEM_SEPARATOR,
                elapsed=self._phase_seconds("\0system" + phase),
            )
        phase, detail = status_parts(self.status)
        call = self.tools.active
        running = self.tools.running
        if call is not None and call.settled is None:
            # A tool's label is already a verb (`Run shell`, `Read file`), so
            # one call is its own phase; parallel calls are counted instead.
            # The clock is the call's own, whatever the model said last.
            self._phase_seconds("")
            line = call.line(timed=False)
            if running < 2:
                phase, _, detail = line.partition(" · ")
            else:
                phase, detail = f"Running {running} tools", line
            return StatusLine(phase, detail, tally=tally, elapsed=call.elapsed)
        if phase.startswith("Running") and not running and not self.user_command:
            # Written for a call that has since finished; the model has the turn.
            phase, detail = "Waiting for model", ""
        line = StatusLine(phase, detail, tally=tally, elapsed=self._phase_seconds(phase))
        if call is not None:
            # Just finished: held briefly and marked done, so a burst of fast
            # calls reads as progress rather than strobing.
            line.detail = f"{'✗' if call.failed else '✓'} {call.line(timed=False)}"
            line.settled = True
        return line

    def status_fragments(self, spinner: str, width: int, tally: str = ""):
        """The row above the tasks: `⠋ Phase · detail … ✓7 tools · 12s`."""
        return self.status_line(tally).fragments(spinner, width)

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


def command_heading(activity: Activity, event: CommandOutput) -> str:
    """Name a running command the way scrollback will name it once it settles.

    Same marker, title and elapsed layout as `CommandTranscript`; only the
    marker differs, because nothing has finished yet. A watched job and a
    `!command` typed at the prompt have no live tool call to name them.
    """
    call = next((c for c in activity.tools.calls if c.event.call_id == event.call_id), None)
    if call is not None:
        purpose = plain(call.event.purpose, limit=60) if call.event.purpose else ""
        name = label(call.event.name)
        return block_heading(
            RUNNING, f"{name} · {purpose}" if purpose else name, monotonic() - call.started
        )
    if event.call_id.startswith(WATCHED_PREFIX):
        return block_heading(RUNNING, f"Job {event.call_id[len(WATCHED_PREFIX) :]}")
    return block_heading(RUNNING, "Shell")


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


# DEC private mode 2026: the terminal holds the frame between these, so the
# editor's erase, the transcript write, and the repaint appear as one change.
# Terminals without it ignore both sequences.
SYNC_START = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"


class Handoff:
    """What the body of an atomic ``suspended_editor`` reports for the repaint.

    ``top_row`` is the 1-based terminal row the erase left the cursor on; the
    body overrides it when it homes the cursor itself. ``rows_written`` is how
    many rows the body's output advanced the cursor; leaving it ``None`` falls
    back to asking the terminal where the cursor ended up.
    """

    def __init__(self, top_row: int | None):
        self.top_row = top_row
        self.rows_written: int | None = None


def layout_top_row(app: Application) -> int | None:
    """The terminal row the editor starts on, if the renderer still knows it."""
    renderer = app.renderer
    if (
        renderer._in_alternate_screen
        or renderer._min_available_height <= 0
        or renderer._last_size != app.output.get_size()
    ):
        return None
    return renderer.rows_above_layout + 1


@asynccontextmanager
async def suspended_editor(app: Application, *, atomic: bool = False):
    """Hand the terminal to direct output, then repaint the editor exactly once.

    With ``atomic`` the handoff is wrapped in synchronized output and, when the
    body reports how many rows it wrote (``Handoff.rows_written``), the editor
    is repainted immediately from the computed cursor row instead of after a
    cursor position report. The whole erase, write, and repaint then reach the
    terminal as one frame with no await in between, so the editor and live
    panel never visibly disappear. A report is still requested and lands
    before the next handoff; the renderer's cursor bookkeeping is relative, so
    a miscount (Rich and the terminal disagreeing on a glyph's width) only
    misjudges the free rows below the editor until that reply corrects it,
    exactly as prompt_toolkit tolerates a layout taller than its report.
    Popups and external programs must not use ``atomic``: a frame held across
    them would freeze the terminal until its guard timeout.

    An atomic handoff also keeps the terminal in raw mode. Only an external
    program needs cooked mode, and in cooked mode the tty echoes anything typed
    during the handoff and turns a Return into a newline, which the editor then
    reads as Ctrl+J. Paced scrollback makes handoffs frequent while the user
    may well be typing, so the window has to be harmless.

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
        yield Handoff(None)
        return
    # Chain to any handoff already in progress, as in_terminal does.
    previous = app._running_in_terminal_f
    done: Future[None] = Future()
    app._running_in_terminal_f = done
    try:
        if previous is not None:
            await previous
        if app.output.responds_to_cpr:
            # Also collects the report an atomic exit left outstanding, before
            # cooked mode could echo it onto the screen as text.
            await app.renderer.wait_for_cpr_responses()
        handoff = Handoff(layout_top_row(app) if atomic else None)
        if atomic:
            app.output.write_raw(SYNC_START)  # erase() flushes it
        app.renderer.erase()
        app._running_in_terminal = True
        # A popup takes over SIGWINCH, but the editor's ``_poll_output_size``
        # task keeps calling ``_on_resize`` on any size change. That erases
        # from the cursor down (over the popup) and requests a CPR the popup
        # then reads as input. Ignore resizes until the handoff repaints below.
        app._on_resize = lambda: None
        try:
            with app.input.detach(), nullcontext() if atomic else app.input.cooked_mode():
                yield handoff
        finally:
            del app._on_resize
            app.renderer.reset()
            if handoff.top_row is not None and handoff.rows_written is not None:
                rows = app.output.get_size().rows
                row = min(handoff.top_row + handoff.rows_written, rows)
                app.renderer._min_available_height = rows - row + 1
                # Sent with the cursor still on the editor's top row, as the
                # renderer requires; the reply only refines the guess above.
                app._request_absolute_cursor_position()
                app._running_in_terminal = False
                app._redraw()
                app.output.write_raw(SYNC_END)
                app.output.flush()
            else:
                if atomic:
                    # Nothing to hold the frame for across the report round trip.
                    app.output.write_raw(SYNC_END)
                    app.output.flush()
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


# Frames a paced backlog takes to drain, so a settled block rolls out row by
# row when small and lands within about a second however big it is.
PACED_DRAIN_FRAMES = 30
# Typed prose advances every TYPED_STEP_FRAMES frames (every frame: 30 a
# second). A step reveals TYPED_CHARS_PER_STEP characters (about 360 a second,
# near a model's own pace, so a steady stream reads as one), sped up so a
# backlog types out within TYPED_DRAIN_STEPS steps (about two seconds).
TYPED_STEP_FRAMES = 1
TYPED_CHARS_PER_STEP = 12
TYPED_DRAIN_STEPS = 60

# Rich writes only SGR styles and OSC 8 hyperlinks. prompt_toolkit's ANSI parser
# knows SGR but would print an OSC's payload as text, so the live row drops them.
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
ESCAPES = re.compile(OSC.pattern + r"|\x1b\[[0-?]*[ -/]*[@-~]")
# Blocks whose rows only make sense whole: typing them out character by
# character would show a half-drawn rule, table border, or code line.
UNTYPED_TOKENS = frozenset({"fence", "code_block", "table_open", "html_block", "hr"})


def typed_prose(objects: tuple) -> bool:
    """Whether a write is prose that reads well typed out rather than rolled in."""
    if not objects:
        return False
    for obj in objects:
        if isinstance(obj, Markdown):
            tokens = obj.parsed
        elif isinstance(obj, ThinkingMarkdown):
            tokens = Markdown(obj.source).parsed
        else:
            return False
        if any(token.type in UNTYPED_TOKENS for token in tokens):
            return False
    return True


@dataclass
class QueuedRow:
    """One rendered row waiting for scrollback.

    ``visible`` counts what a reader sees, without escapes or the trailing
    padding Rich adds, so a blank row costs nothing to reveal. ``lead`` is its
    leading indentation, shown at once so a centred heading or indented list
    item does not spend frames typing spaces. Both are computed on first use:
    a replay queues every row but writes them all without asking.
    """

    text: str
    typed: bool = False

    @cached_property
    def _plain(self) -> str:
        return ESCAPES.sub("", self.text).rstrip()

    @property
    def visible(self) -> int:
        return len(self._plain)

    @property
    def lead(self) -> int:
        return len(self._plain) - len(self._plain.lstrip())


def split_rows(text: str) -> list[str]:
    """Split rendered output into rows, each keeping its newline.

    Only ``\\n`` ends a row; ``str.splitlines`` would also split on control
    characters that escape sequences never contain but text could.
    """
    rows = text.split("\n")
    last = rows.pop()
    result = [row + "\n" for row in rows]
    if last:
        result.append(last)
    return result


class RowCounter:
    """Tee for the Rich console's file that counts the rows a batch advances.

    Rich wraps every line to the width it is given and ends it with a newline,
    so newlines are terminal rows as long as Rich and the terminal agree on
    cell widths; see ``suspended_editor`` for what a disagreement costs.
    """

    def __init__(self, file):
        self.file = file
        self.rows = 0

    def write(self, text: str) -> int:
        self.rows += text.count("\n")
        return self.file.write(text)

    def __getattr__(self, name):
        return getattr(self.file, name)


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
        self.code_theme = code_theme or (lambda: "ansi_dark")
        self.rich_theme = rich_theme or PALETTES["dark"].rich_theme
        self.pending: list[tuple[tuple[object, ...], str, bool]] = []
        self.transient_pending: list[tuple[tuple[object, ...], str, bool]] = []
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.commit_user = self.user
        self.commit_message = self.message
        self.commit_thinking = lambda text: self.print(
            ThinkingMarkdown(text, code_theme=self.code_theme(), style="dim"), end=""
        )
        self._thinking_tail = ""
        self._thinking_streamed = False
        self._regenerate = None
        self.resize_replay = None
        # Rendered rows not yet written. Pacing rolls a settled block out over
        # successive frames instead of landing it in one; off writes them all.
        self.rows: list[QueuedRow] = []
        self.paced = False
        # Typing reveals prose rows a few characters per frame in the live area
        # and writes each to scrollback once it is complete. Needs ``paced``.
        self.typed = False
        # Characters of ``rows[0]`` already shown in the live area.
        self._typed = 0
        self._preview: tuple[tuple[str, int], StyleAndTextTuples] | None = None
        # Frames skipped since the last typing step; see TYPED_STEP_FRAMES.
        self._skipped = 0
        # Rows and characters per step for the current backlog. Fixed when rows
        # arrive rather than recomputed as they leave, or the tail would crawl.
        self._reveal_rate = 0
        self._type_rate = 0
        # Transient writes already rendered into ``rows`` but not yet written,
        # with the absolute index just past their last row. A replay drops the
        # queued rows, so these must join it, just as ``transient_pending`` does.
        self._queued_transient: list[tuple[tuple, int]] = []
        # Absolute index of ``rows[0]``: rows written or dropped so far.
        self._row_base = 0

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
        self.commit_user(prompt)

    def user(self, prompt: str) -> None:
        self.print()
        self.print(TaskPrompt(prompt))
        self.print()

    def message(self, source: str) -> None:
        self.print(Markdown(source, code_theme=self.code_theme()))
        self.print()

    def end_turn(self) -> None:
        self.finish_thinking()
        self.finish()

    def _commit(self, source: str) -> None:
        if source.strip():
            self.commit_message(source)

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

    def _enqueue(self, pending: list, width: int, *, replay: bool = False) -> None:
        """Render writes into rows, marking the prose ones to type out.

        A row is a self-contained unit (Rich closes styles and links per
        segment), so any prefix of the queue can be written now and the rest on
        later frames without splitting an escape. Each write renders on its own
        so its rows know whether they came from prose. A replay is written whole
        at once, so it skips both the prose check and transient tracking.
        """
        transient = [] if replay else self.transient_pending
        self.transient_pending = []
        pieces = []
        previous_file = self.console._file
        try:
            with self.console.use_theme(self.rich_theme()):
                for entry in pending:
                    objects, end, soft_wrap = entry
                    self.console.file = rendered = StringIO()
                    with self.console:
                        self.console.print(*objects, end=end, soft_wrap=soft_wrap, width=width)
                    typed = self.typed and not replay and typed_prose(objects)
                    pieces.append((entry, rendered.getvalue(), typed))
        finally:
            self.console.file = previous_file
        # A write can end mid-row and the next complete it; the row is typed
        # only if every write it holds is.
        text, typed = "", True
        for entry, piece, piece_typed in pieces:
            for part in split_rows(piece):
                text, typed = text + part, typed and piece_typed
                if text.endswith("\n"):
                    self.rows.append(QueuedRow(text, typed))
                    text, typed = "", True
            if any(entry is note for note in transient):
                # Its last character is in the open row, or the one just closed.
                last = len(self.rows) + (1 if text else 0)
                self._queued_transient.append((entry, self._row_base + last))
        if text:
            self.rows.append(QueuedRow(text, typed))

    def _take_pending(self) -> list:
        pending, self.pending = self.pending, []
        return pending

    def _written(self, count: int) -> None:
        """Drop ``count`` rows from the queue's front as written or replaced."""
        self.rows = self.rows[count:]
        self._row_base += count
        self._queued_transient = [
            (entry, end) for entry, end in self._queued_transient if end > self._row_base
        ]

    def _advance(self) -> tuple[int, int]:
        """One paced frame: whole rows now due, and characters shown of the next.

        Rows roll in one at a time and prose types out a few characters a step;
        both speed up so a backlog never falls far behind. Blank rows are free,
        so the gap between paragraphs does not stall the reveal, and so is a
        typed row's indentation.
        """
        self._reveal_rate = max(self._reveal_rate, -(-len(self.rows) // PACED_DRAIN_FRAMES))
        if self._typed and self.rows[0].typed and self._skipped < TYPED_STEP_FRAMES - 1:
            # Mid-row and between steps: nothing moves this frame.
            self._skipped += 1
            return 0, self._typed
        self._skipped = 0
        backlog = sum(row.visible for row in self.rows if row.typed) - self._typed
        self._type_rate = max(
            self._type_rate, TYPED_CHARS_PER_STEP, -(-backlog // TYPED_DRAIN_STEPS)
        )
        rows, chars, typed = self._reveal_rate, self._type_rate, self._typed
        due = 0
        for row in self.rows:
            if row.typed:
                typed = max(typed, row.lead)
                if row.visible - typed > chars:
                    return due, typed + chars
                chars -= row.visible - typed
                typed = 0
            elif row.visible:
                if not rows:
                    break
                rows -= 1
            due += 1
        return due, 0

    def typing_fragments(self) -> StyleAndTextTuples:
        """The prose row being typed out, as far as it has got."""
        if not self._typed or not self.rows:
            return []
        row = self.rows[0]
        key = (row.text, self._typed)
        if self._preview is None or self._preview[0] != key:
            fragments, left = [], self._typed
            for style, text in ANSI(OSC.sub("", row.text.rstrip("\n"))).__pt_formatted_text__():
                if left <= 0:
                    break
                fragments.append((style, text[:left]))
                left -= len(text)
            self._preview = (key, fragments)
        return self._preview[1]

    async def flush(self, *, drain: bool = False) -> None:
        """Write queued output; ``drain`` writes it all, as before a popup or exit."""
        async with self.lock:
            paced = self.paced and not drain
            due = typed = 0
            if paced and self._regenerate is None:
                # Decide what this frame reveals before paying for a handoff: a
                # frame that only types further repaints the live row instead.
                if self.pending:
                    width = max(1, self.app.output.get_size().columns)
                    self._enqueue(self._take_pending(), width)
                if self.rows:
                    due, typed = self._advance()
                if not due and typed != self._typed:
                    self._typed = typed
                    self.app.invalidate()
            if self._regenerate is not None or due or not paced and (self.pending or self.rows):
                # Renderer.reset() shows the cursor at the transcript position
                # both when erasing and before repainting. Suppress those shows
                # until the handoff has restored the editor and its cursor.
                with self.app.output.hidden_cursor():
                    async with suspended_editor(self.app, atomic=True) as handoff:
                        # Snapshot after entering: input/model events can arrive while
                        # the handoff waits for CPR, but not during these sync writes.
                        width = max(1, self.app.output.get_size().columns)
                        if self._regenerate is not None:
                            # Transient writes are not in the replay; keep those
                            # still queued as rows as well as those not rendered.
                            notes = [entry for entry, _ in self._queued_transient]
                            pending = self._regenerate() + notes + self.transient_pending
                            self._regenerate = None
                            self.pending.clear()
                            # Replay covers the rows still queued; drop them.
                            self._written(len(self.rows))
                            self._queued_transient = []
                            # The handoff has erased the editor. Clear the
                            # normal-screen history and home before replay; its
                            # exit will request fresh CPR and restore the draft.
                            self.app.output.write_raw("\x1b[H\x1b[2J\x1b[3J")
                            self.app.output.flush()
                            handoff.top_row = 1
                            self._enqueue(pending, width, replay=True)
                            # A rebuilt screen lands whole; pacing is for new output.
                            due, typed = len(self.rows), 0
                        else:
                            self._enqueue(self._take_pending(), width)
                            if not paced:
                                due, typed = len(self.rows), 0
                        # Rows arriving during the handoff queue behind this frame's.
                        chunk = self.rows[:due]
                        self._written(due)
                        self._typed = typed
                        counter = RowCounter(self.console.file)
                        counter.write("".join(row.text for row in chunk))
                        counter.flush()
                        handoff.rows_written = counter.rows
                # The handoff already repainted the editor on exit.
            self.changed.clear()
            if self.rows:
                # Keep the run loop ticking at its frame rate until drained.
                self.changed.set()
            else:
                self._reveal_rate = self._type_rate = self._typed = self._skipped = 0

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
# Same width as PROMPT_PREFIX, so wrapping is unchanged when the draft flips.
SHELL_PROMPT_PREFIX = "$ "
CONTINUATION_PREFIX = "· "


def prompt_prefix(text: str) -> str:
    """The prompt marker for a draft: `$` once it is a `!command`, else the chevron."""
    return SHELL_PROMPT_PREFIX if text.lstrip().startswith(SHELL_PREFIX) else PROMPT_PREFIX


def _completes_while_typing() -> bool:
    text = get_app().current_buffer.text
    return (
        (text.startswith("/") and "\n" not in text)
        # A leading `$MODEL` or `+EFFORT` while it is still the only word.
        or (text.startswith(("$", "+")) and not any(char.isspace() for char in text))
        or reference_fragment(get_app().current_buffer.document.text_before_cursor) is not None
    )


def _fit_editor(session: PromptSession) -> Window:
    """The session's editor window, sized to its content and without search."""
    # Retain PromptSession's editor/processors, but give its frame a content-sized
    # height. The default frame expands into the CPR-reported space below the
    # cursor, which can be almost the whole pane after a tmux split.
    editor = session.layout.current_window
    editor.height = None
    editor.dont_extend_height = Always()
    # No incremental search here: dropping the editor's search control makes
    # prompt_toolkit's `control_is_searchable` false, so Ctrl+R, Ctrl+S, and vi's
    # `/` and `?` never open an `I-search:` prompt this layout has no room for.
    editor.content._search_buffer_control = None
    return editor


def _per_render(method):
    """Cache a layout callback for the length of one redraw.

    Layout callbacks are queried repeatedly during a single synchronous redraw.
    Never retain their results across redraws: editor/menu/CPR and mutable
    activity state can all change without going through one revision counter.
    """

    @wraps(method)
    def cached(self, *args):
        if self.render_cache is None:
            return method(self, *args)
        key = (method, self.size(), args)
        if key not in self.render_cache:
            self.render_cache[key] = method(self, *args)
        return self.render_cache[key]

    return cached


def _preview_body(diff: bool, body: str, width: int, theme: str):
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


def _spinner_rows(fragments, height) -> VSplit:
    """Chrome rows outside a frame: spinners, notices and queued prompts.

    One column of left padding so every row lines up with the task rows
    inside the frame below instead of sitting against the terminal edge.
    """
    return VSplit(
        [
            Window(width=1),
            Window(
                FormattedTextControl(fragments, show_cursor=False),
                height=height,
                wrap_lines=False,
                dont_extend_height=True,
            ),
        ],
        height=height,
    )


class PromptLayout:
    """The live block under scrollback: activity rows, tasks, previews and the editor.

    Row callbacks read the session's current app on every call, since a
    transcript prompt swaps in its own Application after the layout is built.
    """

    def __init__(
        self,
        session: PromptSession,
        activity: Activity,
        transcript: "Transcript | None",
        shortcuts: PrefixKeys,
    ):
        self.session = session
        self.activity = activity
        self.transcript = transcript
        self.shortcuts = shortcuts
        self.editor = _fit_editor(session)
        self.render_cache = None
        self.animation_task = None
        self.preview_body = lru_cache(maxsize=1)(_preview_body)
        # One spinner for everything live: the status row, the active task, side
        # questions and waits all show the same frame, so motion only ever means
        # "the turn is waiting on this". Who owns the work is the badge and colour.
        self.spinner = Spinner("dots")
        # Every frame is a full layout pass (~2-3ms), so the animation loop alone
        # costs a few percent of a core for the length of a turn. Rich's built-in
        # interval is tuned for a dedicated terminal spinner, not for driving
        # pcode's whole bottom block; slow it ~1.6x, which still reads as motion
        # but noticeably cuts render frequency.
        self.spinner.interval = round(self.spinner.interval * 1.6)
        self.menu = CompletionsMenu(
            max_height=20, scroll_offset=1, extra_filter=has_focus(session.default_buffer)
        )
        self.menu.content.dont_extend_height = Always()
        # Whether the open menu made the layout taller than the measured rows.
        self.menu_overflowed = False

    def size(self):
        return self.session.app.output.get_size()

    # Heights and rows, each computed at most once per redraw.

    @_per_render
    def frame_height(self) -> int:
        """The editor box, including any tasks drawn inside it above a divider."""
        tasks = self.attached_height()
        live = self.preview_layout()
        if live is not None:
            return live[3] + tasks
        size = self.size()
        available = max(1, size.rows - 4 - self.activity_height() - tasks - len(self.queue_rows()))
        cap = self.activity.height_cap(size.rows)
        if cap is not None:
            available = min(available, max(1, cap - 2 - self.tasks_height()))
        text_height = self.editor.preferred_height(max(1, size.columns - 2), available).preferred
        return min(text_height, available) + 2 + tasks

    def refresh_interval(self) -> float:
        """Seconds until the next frame."""
        return self.spinner.interval / 1000

    def spinner_frame(self) -> str:
        return self.spinner.render(monotonic()).plain

    @_per_render
    def base_plan_rows(self, budget: int | None = None):
        if budget is None:
            rows = self.size().rows
            cap = self.activity.height_cap(rows)
            budget = (
                min(10, max(1, rows // 2 - 2))
                if cap is None
                # The editor box keeps one text row inside its two borders.
                else max(1, cap - self.task_chrome() - 3)
            )
        return self.activity.plan_rows(budget, self.spinner_frame())

    @_per_render
    def preview_layout(self):
        """Allocate actual chrome/editor height first, then give output the remainder.

        Keep the normal task viewport unless it would leave no output at all.
        Only in that case trim task rows to preserve a one-line output tail.
        Calculate all three heights together so editor wrapping cannot create a
        circular dependency between frame_height and command_rows.
        """
        activity, transcript = self.activity, self.transcript
        if transcript is None:
            return None
        edits = transcript.show_edits and activity.edit_previews
        # A `!command` the user typed is shown while it runs whatever the
        # scrollback setting for the model's commands says, and so is a job
        # the user asked to watch; the model's own commands follow the setting.
        commands = activity.command_outputs
        if not (transcript.command_scrollback or activity.user_command):
            commands = {
                key: event for key, event in commands.items() if key.startswith(WATCHED_PREFIX)
            }
        if not edits and not commands:
            return None
        size = self.size()
        width = max(1, size.columns - 2)
        # One terminal row stays free for the non-full-screen renderer/CPR.
        fixed = (
            1
            + int(self.session.bottom_toolbar is not None)
            + self.status_height()
            + len(self.queue_rows())
            + self.menu.preferred_height(size.columns, size.rows).preferred
        )
        room = max(0, size.rows - fixed)
        # Parallel calls share the preview; show the most recently updated call.
        event = next(reversed((activity.edit_previews if edits else commands).values()))
        # A sandboxed snippet is pending arguments like an edit, but it is code
        # rather than a diff: no +/- coloring, and nothing has run yet.
        code = bool(edits) and event.kind == "code"
        heading = (
            block_heading(
                RUNNING,
                "Preparing code · not yet run"
                if code
                else f"Preparing edit · {event.path} · not applied",
            )
            if edits
            else command_heading(activity, event)
        )
        plans = self.base_plan_rows()
        # Editor: two borders and at least one text row. Preview: two rule
        # lines, the command, and at least one output row. Keep one task when
        # possible.
        # Attached tasks share the editor's top border and add only a divider.
        chrome_rows = 1 if activity.attach_tasks else 2
        task_floor = 1 + chrome_rows if plans else 0
        editor_room = max(1, room - 2 - 4 - task_floor)
        cap = activity.height_cap(size.rows)
        if cap is not None:
            tasks = len(plans) + chrome_rows if plans else 0
            editor_room = min(editor_room, max(1, cap - 2 - tasks))
        editor_rows = min(editor_room, self.editor.preferred_height(width, editor_room).preferred)
        editor_height = editor_rows + 2
        plan_budget = max(0, room - editor_height - 4 - chrome_rows)
        if len(plans) > plan_budget:
            plans = self.base_plan_rows(plan_budget)
        plan_height = len(plans) + chrome_rows if plans else 0
        # Chrome: the two rule lines, plus the indented `$ command` a shell
        # preview repeats below its heading, exactly as scrollback does.
        chrome = 2 if edits else 3
        budget = min(transcript.command_preview_lines, room - editor_height - plan_height - chrome)
        if budget <= 0:
            return plans, "", [], editor_height
        body = event.text if edits else event.output
        rows = self.preview_body(bool(edits) and not code, body, width, transcript.code_theme)
        if edits:
            # A diff keeps its +/- gutter flush left, as the settled block does.
            return plans, heading, rows[-budget:], editor_height
        block = [("class:plan", "$ " + command_preview(event.command)), *rows[-budget:]]
        return plans, heading, [(style, INDENT + text) for style, text in block], editor_height

    def plan_rows(self):
        live = self.preview_layout()
        return live[0] if live is not None else self.base_plan_rows()

    def preview_heading(self):
        live = self.preview_layout()
        return live[1] if live is not None else ""

    def command_rows(self):
        live = self.preview_layout()
        return live[2] if live is not None else []

    @_per_render
    def notice_rows(self):
        """Freeze the expiring notice for this render so height matches content.

        A waiting leader's hint borrows the slot: it answers a keystroke and
        goes with the next one, which is what a notice is for.
        """
        width = self.size().columns - 1
        if self.shortcuts.pending:
            return chrome_rows(self.shortcuts.hint_text(), width, "class:activity.system")
        return self.activity.notice_rows(width)

    @_per_render
    def job_rows(self):
        return self.activity.job_rows(JOB_ROWS)

    @_per_render
    def group_rows(self):
        """The run's group line so far, when no status row carries its tally.

        While a turn runs the count rides the status row instead, next to the
        spinner that says it is still going; the full line reaches scrollback
        when the run closes.
        """
        if self.transcript is None or self.activity.status_shown:
            return []
        row = self.transcript.pending_group_row(self.size().columns - 1)
        return [("class:activity.group", row)] if row else []

    @_per_render
    def thought_row(self):
        return self.activity.thought_fragments(self.size().columns - 1)

    @_per_render
    def aside_rows(self):
        return self.activity.aside_rows(self.spinner_frame(), self.size().columns - 1)

    @_per_render
    def wait_rows(self):
        """The terminal's own wait (the host starting, a command running there)."""
        return self.activity.wait_fragments(self.spinner_frame(), self.size().columns - 1)

    @_per_render
    def typing_row(self):
        """Prose still being typed out: the next scrollback row, drawn live."""
        output = self.transcript.output if self.transcript is not None else None
        return output.typing_fragments() if output is not None else []

    @_per_render
    def queue_rows(self):
        budget = min(4, max(1, self.size().rows // 4))
        return self.activity.queue_rows(budget)

    def status_gap(self) -> bool:
        """Whether the live panel needs its own blank row above it.

        Scrollback separates blocks with a blank row, but the panel is not
        scrollback: without this the spinner sits flush against the last tool
        line. Depend only on state preview_layout already reads, so asking for
        the gap cannot re-enter the layout calculation.
        """
        shown = (
            self.activity.status_shown
            or bool(self.group_rows())
            or bool(self.notice_rows())
            or bool(self.wait_rows())
            or bool(self.aside_rows())
            or bool(self.job_rows())
        )
        # A typed row is not written yet, so scrollback's own gap sits above it.
        transcript = self.transcript
        return (
            shown
            and transcript is not None
            and (bool(self.typing_row()) or not transcript.ends_blank)
        )

    def status_height(self) -> int:
        return (
            bool(self.typing_row())
            + self.activity.status_shown
            + len(self.thought_row())
            + len(self.group_rows())
            + len(self.notice_rows())
            + len(self.wait_rows())
            + len(self.aside_rows())
            + len(self.job_rows())
            + self.status_gap()
        )

    def task_chrome(self) -> int:
        """Rows the widget adds around its tasks: a divider attached, else a frame."""
        return 1 if self.activity.attach_tasks else 2

    def tasks_height(self) -> int:
        """The widget's full height, wherever it is drawn."""
        rows = self.plan_rows()
        return len(rows) + self.task_chrome() if rows else 0

    def plan_attached(self) -> bool:
        return self.activity.attach_tasks and bool(self.plan_rows())

    def attached_height(self) -> int:
        """Rows attached tasks add to the editor box: the tasks plus a divider."""
        return len(self.plan_rows()) + 1 if self.plan_attached() else 0

    def activity_height(self) -> int:
        rows = [] if self.activity.attach_tasks else self.plan_rows()
        commands = self.command_rows()
        return (
            self.status_height()
            + (len(rows) + 2 if rows else 0)
            + (len(commands) + 2 if commands else 0)
        )

    def plan_text(self):
        return panel_fragments(self.plan_rows(), self.size().columns - 2)

    # Containers.

    def panel_rows(self, rows) -> ConditionalContainer:
        """Padded chrome rows for one row source, shown while it has any."""
        return ConditionalContainer(
            _spinner_rows(
                lambda: panel_fragments(rows(), self.size().columns - 1),
                lambda: len(rows()),
            ),
            filter=Condition(lambda: bool(rows())),
        )

    def plan_body(self) -> Window:
        return Window(
            FormattedTextControl(self.plan_text),
            height=lambda: len(self.plan_rows()),
            dont_extend_height=True,
            wrap_lines=False,
        )

    def plan_heading_border(self) -> VSplit:
        """A top border with the heading at the left; Frame can only center it."""
        activity = self.activity
        return VSplit(
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
                        self.size().columns - 8,
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

    def commands_block(self) -> ConditionalContainer:
        # Framed the way scrollback frames the same run once it settles: the
        # heading rides the opening rule, the body is indented, a rule closes it.
        return ConditionalContainer(
            HSplit(
                [
                    VSplit(
                        [
                            Label(
                                lambda: panel_fragments(
                                    [("class:block.heading", self.preview_heading())],
                                    self.size().columns - 4,
                                ),
                                style="class:block.heading",
                                dont_extend_width=True,
                            ),
                            Window(FormattedTextControl(" "), width=1, style="class:block.rule"),
                            Window(char=RULE, style="class:block.rule"),
                        ],
                        height=1,
                    ),
                    Window(
                        FormattedTextControl(
                            lambda: panel_fragments(self.command_rows(), self.size().columns),
                            show_cursor=False,
                        ),
                        height=lambda: len(self.command_rows()),
                        dont_extend_height=True,
                        wrap_lines=False,
                    ),
                    Window(char=RULE, height=1, style="class:block.rule"),
                ],
                height=lambda: len(self.command_rows()) + 2,
            ),
            filter=Condition(lambda: bool(self.command_rows())),
        )

    def activity_panel(self) -> HSplit:
        """Everything live between scrollback and the editor, top to bottom."""
        activity = self.activity
        transcript = self.transcript
        current_status = ConditionalContainer(
            _spinner_rows(
                lambda: activity.status_fragments(
                    self.spinner_frame(),
                    self.size().columns - 1,
                    transcript.pending_tally() if transcript is not None else "",
                ),
                1,
            ),
            filter=Condition(lambda: activity.status_shown),
        )
        # Its own row, so a running tool taking the status row never hides it.
        thought = ConditionalContainer(
            _spinner_rows(self.thought_row, 1), filter=Condition(lambda: bool(self.thought_row()))
        )
        plan_frame = Frame(self.plan_body(), height=lambda: len(self.plan_rows()) + 2)
        plan_frame.container.children[0] = self.plan_heading_border()
        plan = ConditionalContainer(
            plan_frame,
            filter=Condition(lambda: bool(self.plan_rows()) and not activity.attach_tasks),
        )
        # Keep the turn and its activity adjacent even when the root layout justifies
        # the transcript and editor across the remaining terminal height.
        commands = self.commands_block()
        status_spacer = ConditionalContainer(Window(height=1), filter=Condition(self.status_gap))
        # Flush left, where scrollback will draw the same line once the run closes.
        group = ConditionalContainer(
            Window(
                FormattedTextControl(self.group_rows, show_cursor=False),
                height=lambda: len(self.group_rows()),
                wrap_lines=False,
                dont_extend_height=True,
            ),
            filter=Condition(lambda: bool(self.group_rows())),
        )
        # Directly above the spinner: a notice answers the keystroke that caused it
        # without ever reaching scrollback, and vanishes on its own.
        notice = self.panel_rows(self.notice_rows)
        # This terminal's own wait on the session host, hidden while a turn row covers it.
        waits = self.panel_rows(self.wait_rows)
        # Below the spinner: side questions run beside the turn and outlive it, so
        # they get their own spinner rows rather than a share of the prompt's.
        asides = self.panel_rows(self.aside_rows)
        # What is running that the spinner does not cover.
        # Shown while idle too, which is when "is the suite still going?" is asked.
        jobs = self.panel_rows(self.job_rows)
        return HSplit(
            [
                status_spacer,
                group,
                commands,
                notice,
                current_status,
                thought,
                waits,
                asides,
                jobs,
                plan,
            ]
        )

    def queued(self) -> ConditionalContainer:
        return ConditionalContainer(
            _spinner_rows(
                lambda: panel_fragments(self.queue_rows(), self.size().columns - 1),
                lambda: len(self.queue_rows()),
            ),
            filter=Condition(lambda: bool(self.activity.queued_prompts)),
        )

    def typing(self) -> ConditionalContainer:
        # Flush left like the scrollback row it becomes, and placed above the
        # layout's justifying filler so it sits directly beneath scrollback rather
        # than jumping up a row when it is written.
        return ConditionalContainer(
            Window(
                FormattedTextControl(self.typing_row, show_cursor=False),
                height=1,
                wrap_lines=False,
                dont_extend_height=True,
            ),
            filter=Condition(lambda: bool(self.typing_row())),
        )

    def editor_frame(self) -> Frame:
        session = self.session
        editor_frame = Frame(self.editor, height=self.frame_height)
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
        # Attached tasks: the widget's heading becomes the editor's top border and
        # a divider separates the tasks from the text. frame_height counts both.
        side = partial(Window, char="│", width=1, style="class:frame.border")
        editor_frame.container.children[0] = HSplit(
            [
                ConditionalContainer(
                    HSplit(
                        [
                            self.plan_heading_border(),
                            VSplit([side(), self.plan_body(), side()]),
                            VSplit(
                                [
                                    Window(char="├", width=1, style="class:frame.border"),
                                    Window(char="─", style="class:frame.border"),
                                    Window(char="┤", width=1, style="class:frame.border"),
                                ],
                                height=1,
                            ),
                        ]
                    ),
                    filter=Condition(self.plan_attached),
                ),
                ConditionalContainer(
                    editor_frame.container.children[0], filter=~Condition(self.plan_attached)
                ),
            ]
        )
        return editor_frame

    def bottom_toolbar(self) -> Window:
        return Window(
            FormattedTextControl(
                lambda: self.session.bottom_toolbar, style="class:bottom-toolbar.text"
            ),
            style="class:bottom-toolbar",
            height=1,
        )

    def layout(self) -> Layout:
        children = [self.menu, self.activity_panel(), self.queued(), self.editor_frame()]
        if self.transcript is not None:
            children[:0] = [self.typing(), Window()]
        if self.session.bottom_toolbar is not None:
            children.append(self.bottom_toolbar())
        return Layout(
            HSplit(
                children,
                align=VerticalAlign.JUSTIFY if self.transcript else VerticalAlign.BOTTOM,
            ),
            focused_element=self.editor,
        )

    # Redraw hooks: the per-render cache and the spinner's animation timer.

    def needs_animation(self) -> bool:
        activity = self.activity
        return (
            activity.busy
            or activity.status_shown
            # Keep redrawing while a notice is live: nothing else will ask for
            # the frame that finally removes it.
            or activity.notice_shown
            or activity.asides_running
            or bool(activity.waits)
            or (activity.tasks_shown and activity.tools.animating)
        )

    async def animate(self, app) -> None:
        await asyncio.sleep(self.refresh_interval())
        self.animation_task = None
        # Repaint unconditionally: this timer only exists because the previous
        # render was animated, and the frame that removes an expired notice or
        # a finished spinner is the one nothing else asks for.
        app.invalidate()

    def before_render(self, app) -> None:
        self.render_cache = {}
        transcript, activity = self.transcript, self.activity
        if transcript is None or not (
            (transcript.show_edits and activity.edit_previews)
            or (transcript.command_scrollback and activity.command_outputs)
            or any(key.startswith(WATCHED_PREFIX) for key in activity.command_outputs)
        ):
            self.preview_body.cache_clear()

    def after_render(self, app) -> None:
        self.render_cache = None
        # A redraw caused by input or application events starts animation again.
        # Idle prompts have no timer; toolkit owns cancellation at app shutdown.
        if self.needs_animation() and app.is_running:
            if self.animation_task is None or self.animation_task.done():
                self.animation_task = app.create_background_task(self.animate(app))
        elif self.animation_task is not None:
            self.animation_task.cancel()
            self.animation_task = None
        self.replay_after_menu(app)

    def replay_after_menu(self, app) -> None:
        """Rebuild scrollback, as a resize does, once an overflowing menu closes.

        A completion menu taller than the rows below the editor scrolls the
        terminal, and prompt_toolkit never shrinks its screen again (each render
        keeps at least the last height), so closing the menu leaves blank rows
        under the editor where scrolled-away transcript used to be. The replay
        resets the renderer and repaints that history.
        """
        transcript, renderer = self.transcript, app.renderer
        if transcript is None or not transcript.replays_on_resize:
            return
        screen, available = renderer._last_screen, renderer._min_available_height
        if renderer._in_alternate_screen or screen is None or available <= 0:
            return
        if self.session.default_buffer.complete_state is not None:
            self.menu_overflowed |= screen.height > available
        elif self.menu_overflowed:
            self.menu_overflowed = False
            transcript.regenerate()


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
    on_previous_session=None,
    on_copy_response=None,
    key_prefix: str | None = None,
    **kwargs,
) -> PromptSession:
    configure_newline_keys()
    install_fast_layout_division()
    activity = activity or Activity()
    callbacks = PromptCallbacks(
        on_cancel=on_cancel,
        on_effort=on_effort,
        on_model=on_model,
        on_tasks=on_tasks,
        on_thinking=on_thinking,
        on_commands=on_commands,
        on_send_mode=on_send_mode,
        on_previous_session=on_previous_session,
        on_copy_response=on_copy_response,
    )
    keys, shortcuts = prompt_key_bindings(
        activity, transcript, callbacks, key_prefix, lambda: session.default_buffer
    )

    output = kwargs.pop("output", None)
    if not isinstance(output, CursorSafeOutput):
        output = CursorSafeOutput(output if output is not None else create_output())
    session = PromptSession(
        output=output,
        message=lambda: [("class:prompt", prompt_prefix(session.default_buffer.text))],
        prompt_continuation=lambda width, line, soft: [
            ("class:prompt", "  " if soft else CONTINUATION_PREFIX)
        ],
        # Every prefix is the same width, so wrapped rows all get the same
        # amount of text space.
        input_processors=[WordWrapProcessor(prefix_width=get_cwidth(PROMPT_PREFIX))],
        multiline=True,
        erase_when_done=True,
        completer=merge_completers([SlashCompleter(registry), FileReferenceCompleter(workspace)]),
        lexer=ReferenceLexer(extra=[(MARKER_PATTERN, "class:paste-marker")]),
        complete_while_typing=Condition(_completes_while_typing),
        reserve_space_for_menu=0,
        auto_suggest=AutoSuggestFromHistory(),
        key_bindings=shortcuts.key_bindings(keys),
        mouse_support=False,
        **kwargs,
    )
    session.shortcuts = shortcuts

    prompt_layout = PromptLayout(session, activity, transcript, shortcuts)
    if transcript is not None:

        def accept(buffer):
            text = buffer.text
            buffer.append_to_history()
            on_submit(text)
            return False

        session.default_buffer.accept_handler = accept
    session.layout = prompt_layout.layout()
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
    session.app.before_render += prompt_layout.before_render
    session.app.after_render += prompt_layout.after_render
    if session.app.editing_mode == EditingMode.VI:
        # Allow terminal escape sequences to arrive, without a half-second pause.
        session.app.ttimeoutlen = 0.1
    if transcript is None or not transcript.replays_on_resize:
        install_reflow_renderer(session.app)

    return session


def tally(succeeded: int, failed: int, sep: str = "") -> str:
    """`✓2 ✗1`, dropping a zero side, so each mark only ever counts its own outcome."""
    return " ".join(
        f"{mark}{sep}{count}" for mark, count in (("✓", succeeded), ("✗", failed)) if count
    )


def tool_count(name: str, count: int, failed: int) -> str:
    """One tool's part of a group line: `Read file ✓3`, `Search code ✗1`, `Search code ✓2 ✗1`."""
    return f"{name} {tally(count - failed, failed)}"


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
        preferences: dict[str, str] | None = None,
        detected_theme: str | None = None,
    ) -> None:
        preferences = load_preferences() if preferences is None else preferences
        self.error_scrollback_lines = int(preferences.get("error_scrollback_lines", "20"))
        self.tool_error_scrollback = preferences.get("tool_error_scrollback", "off") == "on"
        self.show_edits = preferences.get("show_edits", "on") == "on"
        self.command_scrollback = preferences.get("show_commands", "off") == "on"
        self.group_tools = preferences.get("group_tools", SETTINGS["group_tools"].default) == "on"
        self.command_scrollback_lines = int(preferences.get("command_scrollback_lines", "20"))
        self.command_preview_lines = int(preferences.get("command_preview_lines", "10"))
        self.activity = activity
        self.console = console
        self.theme = theme
        self.detected_theme = detect_theme() if detected_theme is None else detected_theme
        self.syntax_themes = syntax_themes(preferences)
        self._output: TerminalOutput | None = None
        self.regenerate_on_resize = preferences.get("regenerate_on_resize", "on") == "on"
        self.paced_scrollback = preferences.get(
            "paced_scrollback", SETTINGS["paced_scrollback"].default
        )
        self.log = TranscriptLog(
            max_chars=int(
                preferences.get("transcript_max_chars", SETTINGS["transcript_max_chars"].default)
            )
        )
        self._replay_sink: list | None = None
        self._block: str | None = None
        # A sub-agent's calls settle before its delegate does. They wait here,
        # keyed by the delegate's call id, to be written beneath it.
        self._children: dict[str, list[ToolSummary]] = {}
        # With `group_tools`, the settled calls of the run in progress: they
        # reach scrollback as one line when anything else is written or the
        # turn ends, and the live panel counts them until then.
        self._group: list[ToolSummary] = []

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
            output.commit_user = self.user
            output.commit_message = self.message
            output.commit_thinking = self.thinking
            # Pacing spreads handoffs over frames; only a real application has
            # either, so offline harnesses and stand-ins write at once.
            output.paced = (
                self.paced_scrollback != "off"
                and self.console.is_terminal
                and isinstance(output.app, Application)
            )
            output.typed = output.paced and self.paced_scrollback == "typed"
            if self.replays_on_resize:
                output.resize_replay = self.replay

    @recorded
    def print(self, *objects, end="\n", tool_line: bool = False) -> None:
        """Write scrollback, keeping tool lines one block apart from other output."""
        if self._group:
            # Anything written after a run of calls closes it, so the run's
            # line lands where the calls happened.
            self._flush_group()
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
    def message(self, text: str) -> None:
        """Retain a Markdown block and its separator atomically, even at tiny budgets."""
        self.print(Markdown(text, code_theme=self.code_theme))
        self.print()

    @recorded
    def thinking(self, text: str) -> None:
        """Retain readable provider text, choosing visibility again on every redraw."""
        if self.activity is not None and self.activity.show_thinking:
            self.print(ThinkingMarkdown(command_text(text), code_theme=self.code_theme), end="")

    @recorded
    def tool_result(self, event: ToolSummary) -> None:
        """Retain hidden results too; choose one representation on each replay."""
        if event.parent_call_id:
            if self.summarizes(event):
                self._children.setdefault(event.parent_call_id, []).append(event)
            return
        if self.writes_tool_result(event):
            self.events((event,))
        for line in self.child_lines(self._children.pop(event.call_id, []), CHILD_INDENT):
            self.print(Padding(line, (0, 0, 0, CHILD_INDENT), expand=False), tool_line=True)

    def child_lines(self, children: list[ToolSummary], indent: int = 0) -> list[Text]:
        """A sub-agent's calls, folded the way the parent's are when grouping."""
        if self.group_tools:
            return self.group_lines(children, indent=indent)
        return [line for child in children for line in self.summary_lines(child, indent=indent)]

    def groups(self, event: ToolSummary) -> bool:
        """Report whether this call's summary line folds into the run's group line.

        A delegate heads its own calls, and a background job's exit is a
        delayed notice, not part of the run. A failure folds in like any call.
        """
        return self.group_tools and event.name != DELEGATE and event.execution != "background"

    def group_lines(self, events: list[ToolSummary], *, indent: int = 0) -> list[Text]:
        """Fold a run of calls into one line; a run of one keeps the call's own line."""
        if len(events) == 1:
            return self.summary_lines(events[0], indent=indent)
        if not events:
            return []
        return [self.group_line(events, width=max(1, self.console.width - indent))]

    @staticmethod
    def group_line(events: list[ToolSummary], *, width: int) -> Text:
        """`✓ 15 tools · Edit file ✓10 · Run shell ✓5`, most used first.

        Failures split the run's count and their tool's, so a run that hit
        one still stands out: `✓ 5 ✗ 1 tools · Read file ✓3 · Search code ✓2 ✗1`.
        """
        counts = Counter(label(event.name) for event in events)
        failures = Counter(label(event.name) for event in events if event.failed)
        failed = failures.total()
        parts = [tool_count(name, count, failures[name]) for name, count in counts.most_common()]
        head = tally(len(events) - failed, failed, sep=" ")
        line = Text(f"{head} tools · " + " · ".join(parts), style="pcode.thinking")
        line.no_wrap = True
        line.overflow = "ellipsis"
        line.truncate(width, overflow="ellipsis")
        return line

    def _flush_group(self) -> None:
        group, self._group = self._group, []
        # The retained tool results already reproduce this line on replay.
        recording, self.log.recording = self.log.recording, False
        try:
            for line in self.group_lines(group):
                self.print(line, tool_line=True)
        finally:
            self.log.recording = recording

    def settle_tools(self) -> None:
        """Write the pending group line; the turn ended with nothing after it.

        Not recorded: whatever is written next closes the run at the same
        place, and `replay` closes a trailing one unless it is still live.
        """
        if self._group:
            self._flush_group()

    def pending_group_row(self, width: int) -> str:
        """The group line so far, for the live panel; empty when nothing is pending."""
        if not self._group or width < 1:
            return ""
        line = self.group_lines(self._group)[-1].copy()
        line.truncate(width, overflow="ellipsis")
        return line.plain

    def pending_tally(self) -> str:
        """The open run's count for the status row: `✓7 ✗1 tools`, or empty."""
        if not self._group:
            return ""
        failed = sum(event.failed for event in self._group)
        noun = "tool" if len(self._group) == 1 else "tools"
        return f"{tally(len(self._group) - failed, failed)} {noun}"

    def settle_orphans(self) -> None:
        """Write sub-agent calls whose delegate never settled, e.g. a cancelled turn.

        Without a delegate row to sit under they are written flush, so the
        steps a sub-agent did take are not silently lost.
        """
        orphans, self._children = self._children, {}
        for children in orphans.values():
            for line in self.child_lines(children):
                self.print(line, tool_line=True)

    @recorded
    def edit(self, event) -> None:
        if self.show_edits:
            self.print(EditTranscript(event, code_theme=self.code_theme))

    def replay(self) -> list:
        """Project the retained log with current settings, without recording again."""
        sink = []
        self._replay_sink = sink
        self._block = None
        # Replaying the log's own tool results rebuilds whatever is pending.
        self._children = {}
        live_group, self._group = bool(self._group), []
        self.log.recording = False
        try:
            if self.log.dropped:
                self.note("Earlier transcript entries omitted from this regenerated view.")
            for entry in self.log.entries:
                getattr(self, entry.method)(*entry.args, **entry.kwargs)
            # A run still going stays in the live panel; any other was closed.
            if not live_group:
                self.settle_tools()
        finally:
            self.log.recording = True
            self._replay_sink = None
        return sink

    @contextmanager
    def restore(self):
        """Replace history without rendering discarded entries, then redraw once.

        Saved history goes through the same recorded methods and retention budget
        as live output. Redirected output receives the retained slice once, without
        terminal escapes; subsequent redraw requests remain a no-op there.
        """
        previous = self.log
        self.log = TranscriptLog(limit=previous.limit, max_chars=previous.max_chars)
        self.log.capture_only = True
        # Restored history is settled; nothing from it is still running.
        self._group = []
        try:
            yield
        except BaseException:
            self.log = previous
            raise
        finally:
            self.log.capture_only = False
        if self.output is not None and self.console.is_terminal:
            self.regenerate()
        else:
            for objects, end, _ in self.replay():
                self._write(objects, end)

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
    def terminal_colors(self) -> bool:
        """Whether `/syntax terminal` hands every color to the terminal's ANSI palette."""
        return self.syntax_themes[self.resolved_theme] == TERMINAL_SYNTAX

    @property
    def menu_palette(self) -> Palette:
        """The palette implied by the syntax style in use, for the popup."""
        if self.terminal_colors:
            return TERMINAL_PALETTE
        return syntax_palette(self.syntax_themes[self.resolved_theme], self.palette)

    @property
    def chrome_palette(self) -> Palette:
        """The same style read for text drawn straight onto the terminal.

        The popup brings its own background; the prompt, the plan rows and the
        frame do not, so the palette's surface stands in for the terminal's
        background and any color that would be lost against it is dropped.
        """
        if self.terminal_colors:
            return TERMINAL_PALETTE
        style = self.syntax_themes[self.resolved_theme]
        return syntax_palette(style, self.palette, self.palette.surface)

    def prompt_style(self) -> Style:
        """The prompt_toolkit style for the current theme and syntax style."""
        return self.chrome_palette.prompt_style(self.menu_palette)

    @property
    def rich_theme(self) -> Theme:
        return TERMINAL_THEME if self.terminal_colors else self.palette.rich_theme()

    @property
    def code_theme(self) -> str:
        return (
            f"ansi_{self.resolved_theme}"
            if self.terminal_colors
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
        """Report whether this settled tool is written to scrollback as it arrives.

        A sub-agent's call is not: it waits to be written beneath its delegate.
        """
        return (
            isinstance(event, ToolSummary) and not event.parent_call_id and self.summarizes(event)
        )

    def summarizes(self, event: Event) -> bool:
        """Report whether this settled tool reaches scrollback at all.

        Omit calls whose results already have a home: planning in the task
        panel, shown edits in their diff, and job inspection in the job's own
        completion notice. Actual tool errors still get their own entry; only
        how much of them, a summary or diagnostic, is configurable.
        """
        if not isinstance(event, ToolSummary):
            return False
        # Inspecting/waiting on a job is not another command completion. A
        # nonzero job exit sets `failed`, but the helper call itself succeeded;
        # only actual helper errors/retries need their own scrollback entry.
        if event.name in {"wait_for_job", "job_output"} and event.outcome == "success":
            return False
        # A command always leaves at least the summary line every other tool
        # leaves; `show_commands` governs only its mirrored output.
        if event.name in COMMAND_TOOLS:
            return True
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
        # The job's closing marker repeats the heading's marker and elapsed
        # time; only its id is new, so the heading takes that and drops the rest.
        output, job = split_outcome(output)
        output = output.rstrip("\n")
        if not output.strip():
            output = "(no output)"
        title = label(event.name)
        if event.execution == "background":
            # This is the delayed exit notice, not just a tool result: keep the
            # exact outcome even when the footer is absorbed into the heading.
            title += " · " + plain(event.detail.rsplit(" → ", 1)[-1], limit=None)
        elif job:
            title += f" · {job}"
        self.print(
            CommandTranscript(
                command=invocation,
                output=output,
                # The purpose belongs in the title, not the command line: that
                # line is syntax-highlighted as shell and should stay runnable.
                title=f"{title} · {event.purpose}" if event.purpose else title,
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

    @recorded
    def cancelled(self) -> None:
        self.settle_orphans()
        self.print(TranscriptNotice("", "cancelled", "Run cancelled"))

    @recorded
    def user(self, text: str) -> None:
        self.settle_orphans()
        self.print()
        self.print(TaskPrompt(text))
        self.print()

    def summary_lines(self, event: ToolSummary, *, indent: int = 0) -> list[Text]:
        """The compact rows a settled call leaves in scrollback."""
        if event.name not in COMMAND_TOOLS and not event.command:
            return [
                Text.assemble(
                    (f"{'✗' if event.failed else '✓'} {label(event.name)}  ", "pcode.thinking"),
                    (plain(event.detail, limit=None), "pcode.thinking"),
                    (
                        f"  {event.elapsed_seconds:.1f}s"
                        if event.elapsed_seconds is not None
                        else "",
                        "pcode.thinking",
                    ),
                )
            ]
        # Scrollback shows the outcome only: the live panel already named the
        # target while the call ran. The session browser, which has no such
        # panel, passes the whole detail to the same renderer.
        result = (
            " · " + plain(event.detail.rsplit(" → ", 1)[-1], limit=60)
            if event.failed or (event.name != "run_command" and " → " in event.detail)
            else ""
        )
        return tool_summary_lines(
            event.name,
            result,
            failed=event.failed,
            elapsed_seconds=event.elapsed_seconds,
            command=event.command,
            width=max(1, self.console.width - indent),
        )

    def command_summary(self, event: ToolSummary) -> None:
        """Write a settled call's summary line, or hold it for the run's group line."""
        if self.groups(event):
            self._group.append(event)
            return
        for line in self.summary_lines(event):
            self.print(line, tool_line=True)

    @recorded
    def events(self, events: tuple[Event, ...], *, show_tools: bool = False) -> None:
        for event in events:
            if isinstance(event, CacheBust):
                # Informational, like a retained note: muted, no marker or title.
                self.print(Text(command_text(event.text), style="pcode.muted"))
            elif isinstance(event, Thinking):
                self.thinking(event.text.rstrip("\n") + "\n\n")
            elif isinstance(event, Message):
                self.message(event.markdown)
            elif isinstance(event, ToolSummary):
                if event.name in COMMAND_TOOLS:
                    # Mirroring owns command completions. A call whose
                    # captured output is withheld still reports itself.
                    if not self.command_output(event):
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
                self.command_summary(event)

    def help(self, registry: CommandRegistry, shortcut: Callable[[str], str] | None = None) -> None:
        """List the commands, then the keys; ``shortcut`` spells the prompt's own."""
        key = shortcut or shortcut_label
        previous = key("^") + (" (Ctrl+6)" if key("^") == "Ctrl+^" else "")
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
        self.note("Enter send · ↓ on last line or Ctrl+J newline · Tab/↑/↓ complete")
        self.note("Enter accepts a selected completion; press again to send.")
        self.note(
            f"{key('o')} tasks widget · {key('t')} thinking · {key('g')} command output "
            "(each redraws)"
        )
        self.note(
            f"{key('l')} choose model · {key('n')} raise effort · {key('p')} lower effort "
            "(next turn)"
        )
        self.note(f"{previous} back to the previous session (/switch -)")
        self.note(f"{key('y')} copy the draft · Ctrl+C discard input · Ctrl+D exit on empty input")
        self.note(
            f"During a run: Enter sends · {key('s')} picks steering/queue/interrupt for the "
            "next send. Ctrl+C discards a draft first, then cancels · Ctrl+D cancels, keeps draft."
        )
        self.note("Cancellation clears queued messages. Use terminal/tmux scrollback for history.")
        self.print()
