"""Temporary session popups; the editor is suspended while one owns the terminal."""

from pathlib import Path

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import DynamicContainer, HSplit, Layout, VSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Dialog, Frame, Label, TextArea
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text
from rich.theme import Theme

from pcode.diagnostics import redact
from pcode.popup_ui import (
    RichPane,
    list_pane_height,
    popup_container,
    popup_style,
    steer_list_from_query,
)
from pcode.sessions import SessionInfo, ToolCall, Turn, first_prompt, session_turns
from pcode.task_prompt import TaskPrompt
from pcode.tool_display import plain, tool_summary_lines
from pcode.worktree import repo_scope


def literal(text: str) -> str:
    """Every line of a prompt or response, redacted and safe for the terminal.

    Nothing is dropped: the pane scrolls, and a turn read here should say what
    the turn said. Blank lines inside are structure, so only the surrounding
    ones go, which would otherwise draw an empty quote rail.
    """
    lines = [plain(line, limit=None) for line in redact(text).splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


class SessionBrowser:
    """Full-screen browser over saved sessions: list, per-turn detail, and search.

    Turns are read lazily from each transcript and cached. Typing a query reads
    every session in scope once; a session stays listed only if a turn matches
    every query word (prompts by default, responses too with ``r``).
    """

    def __init__(
        self,
        records: list[SessionInfo],
        *,
        root: Path,
        workspace: Path,
        active_id: str | None = None,
        rich_theme: Theme | None = None,
        code_theme: str = "ansi_dark",
        color_system: str | None = "truecolor",
        **app_options,
    ) -> None:
        self.records = records
        self.root = root
        self._workspace_scopes: dict[Path, Path] = {}
        self.workspace = self.workspace_scope(workspace)
        self.active_id = active_id
        self.code_theme = code_theme
        self.everywhere = False
        self.responses = False
        self.visible: list[SessionInfo] = []
        self.selected: SessionInfo | None = None
        self._turns: dict[str, list[Turn] | None] = {}
        self._titles: dict[str, str] = {}
        self._refreshing = False
        self.query = TextArea(height=1, prompt="Search prompts: ", multiline=False)
        self.list = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        self.list.window.cursorline = Always()
        self.detail = RichPane(theme=rich_theme, color_system=color_system)
        self.query.buffer.on_text_changed += lambda _: self.refresh()
        self.list.buffer.on_cursor_position_changed += lambda _: self.select()
        keys = KeyBindings()
        self.detail.bind_scrolling(keys)

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        @keys.add("c-d", filter=~has_focus(self.detail.window))
        def close(event):
            event.app.exit(result=None)

        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)
        steer_list_from_query(keys, self.query, self.list)

        @keys.add("enter")
        def resume(event):
            # Like the model picker: Enter while typing accepts the selection.
            if self.selected is not None:
                event.app.exit(result=self.selected.id)

        @keys.add("/", filter=has_focus(self.list))
        @keys.add("c-f")
        def search(event):
            event.app.layout.focus(self.query)

        @keys.add("w", filter=has_focus(self.list))
        def workspaces(event):
            self.everywhere = not self.everywhere
            self.refresh()

        @keys.add("r", filter=has_focus(self.list))
        def responses(event):
            self.responses = not self.responses
            self.refresh()

        header = Label(
            lambda: (
                f"Sessions · {len(self.visible)}/{len(self.in_scope())} · "
                f"Workspace: {'all' if self.everywhere else self.workspace.name} · "
                f"Search: {'prompts + responses' if self.responses else 'prompts'}"
            )
        )
        wide = VSplit(
            [
                Frame(self.list, title="Sessions", width=Dimension(weight=2)),
                Frame(self.detail, title="Turns", width=Dimension(weight=3)),
            ]
        )
        narrow = HSplit(
            [
                Frame(
                    self.list,
                    title="Sessions",
                    height=lambda: list_pane_height(len(self.visible)),
                ),
                Frame(self.detail, title="Turns"),
            ]
        )
        body = DynamicContainer(
            lambda: wide if get_app().output.get_size().columns >= 100 else narrow
        )
        root_container = HSplit(
            [
                header,
                self.query,
                body,
                Label("↑↓ Select/scroll · Enter Resume · Tab Focus · Esc Cancel"),
                Label("In Turns: PgUp/PgDn Page · Ctrl+U/D Half page"),
                Label("In Sessions: / Search (↑↓ select while typing) · r Responses too · w All"),
            ]
        )
        self.app = Application(
            layout=Layout(popup_container(root_container), focused_element=self.list),
            key_bindings=keys,
            full_screen=True,
            mouse_support=True,
            style=popup_style(app_options.pop("style", None)),
            **app_options,
        )
        self.refresh()

    def turns(self, info: SessionInfo) -> list[Turn]:
        if info.id not in self._turns:
            self._turns[info.id] = session_turns(info, self.root)
        return self._turns[info.id] or []

    def workspace_scope(self, workspace: Path) -> Path:
        """Group linked checkouts, resolving each workspace only once per browser."""
        workspace = workspace.resolve()
        if workspace not in self._workspace_scopes:
            self._workspace_scopes[workspace] = repo_scope(workspace)
        return self._workspace_scopes[workspace]

    def in_scope(self) -> list[SessionInfo]:
        if self.everywhere:
            return self.records
        return [
            info
            for info in self.records
            if self.workspace_scope(Path(info.workspace)) == self.workspace
        ]

    def matches(self, turn: Turn, words: list[str]) -> bool:
        haystack = turn.prompt.casefold()
        if self.responses:
            # Every text block, not just the final one: a turn's answer is often
            # split by tool calls, and the searched-for sentence can be in any part.
            haystack += "\n" + "\n".join(
                block.casefold() for block in turn.blocks if isinstance(block, str)
            )
        return all(word in haystack for word in words)

    def matching_turns(self, info: SessionInfo) -> list[Turn]:
        words = self.query.text.casefold().split()
        turns = self.turns(info)
        if not words:
            return turns
        return [turn for turn in turns if self.matches(turn, words)]

    def title(self, info: SessionInfo) -> str:
        # Listing reads only the first prompt; the full transcript loads on select/search.
        if info.id not in self._titles:
            marker = "* " if info.id == self.active_id else "  "
            first = plain(redact(first_prompt(info, self.root)), 80)
            self._titles[info.id] = f"{marker}{info.updated[5:16].replace('T', ' ')}  {first}"
        return self._titles[info.id]

    def heading(self, info: SessionInfo, shown: int, total: int) -> str:
        parts = [info.id[:8], plain(info.model, 40), info.updated[:16].replace("T", " ")]
        if info.id == self.active_id:
            parts.append("active")
        noun = "turn" if total == 1 else "turns"
        parts.append(f"{shown} of {total} {noun}" if shown < total else f"{total} {noun}")
        return " · ".join(parts)

    def refresh(self) -> None:
        previous = self.selected
        searching = bool(self.query.text.strip())
        self.visible = [
            info for info in self.in_scope() if not searching or self.matching_turns(info)
        ]
        selected = next((i for i, info in enumerate(self.visible) if info is previous), 0)
        lines = [self.title(info) for info in self.visible]
        text = "\n".join(lines) or "No matching sessions."
        position = sum(len(line) + 1 for line in lines[:selected])
        self._refreshing = True
        self.list.buffer.set_document(Document(text, position), bypass_readonly=True)
        self._refreshing = False
        self.select(force=True)

    def select(self, force: bool = False) -> None:
        if self._refreshing:
            return
        row = self.list.document.cursor_position_row
        info = self.visible[row] if row < len(self.visible) else None
        if info is self.selected and info is not None and not force:
            return
        self.selected = info
        self.detail.set(self.details(info))

    def details(self, info: SessionInfo | None) -> list:
        """Rich renderables for the Turns pane: prompts verbatim, responses as Markdown."""
        if info is None:
            return [Text("No matching sessions.")]
        total = len(self.turns(info))
        if self._turns[info.id] is None:
            return [Text("(Transcript unavailable)")]
        turns = self.matching_turns(info)
        blocks: list = [Text(self.heading(info, len(turns), total), style="dim")]
        if not turns:
            blocks.append(Text("(No prompt yet)"))
        for turn in turns:
            blocks.append(Text(""))
            # The same quote rail scrollback draws, so a remembered turn looks
            # here the way it looked when it was live, blank line included.
            blocks.append(TaskPrompt(literal(turn.prompt)))
            blocks.append(Text(""))
            # Consecutive tool lines stay flush and a blank row marks entering or
            # leaving that run, the rule Transcript.print applies to scrollback.
            previous = "blank"
            for block in turn.blocks:
                if isinstance(block, ToolCall):
                    # The whole detail, unlike scrollback: there is no live tool
                    # panel here to have already named what the call worked on.
                    kind = "tools"
                    rendered = [
                        Padding(line, (0, 0, 0, 2))
                        for line in tool_summary_lines(
                            block.name,
                            " · " + plain(block.detail, limit=None) if block.detail else "",
                            failed=block.failed,
                            elapsed_seconds=block.elapsed_seconds,
                            command=block.command,
                        )
                    ]
                elif text := literal(block):
                    kind = "text"
                    rendered = [Padding(Markdown(text, code_theme=self.code_theme), (0, 0, 0, 2))]
                else:
                    continue
                if previous not in ("blank", kind):
                    blocks.append(Text(""))
                blocks.extend(rendered)
                previous = kind
            if not turn.blocks:
                state = turn.status if turn.status != "complete" else "no response text"
                blocks.append(Text(f"  ({state})", style="dim"))
            elif turn.status not in ("complete", "running"):
                blocks.append(Text(f"  ({turn.status})", style="dim"))
        return blocks

    async def run(self) -> str | None:
        return await self.app.run_async()


def session_info_dialog(rows, *, input=None, output=None, style=None):
    """Read-only view of the live session; every key that closes it exits the same way."""
    width = max((len(label) for label, _ in rows), default=0)
    body = TextArea(
        text="\n".join(f"{label.ljust(width)}  {value}" for label, value in rows),
        read_only=True,
        scrollbar=True,
        focus_on_click=True,
    )
    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    @bindings.add("enter", eager=True)
    @bindings.add("q", eager=True)
    @bindings.add("c-c")
    @bindings.add("c-d")
    def close(event):
        event.app.exit(result=None)

    dialog = Dialog(
        title="Session",
        body=HSplit(
            [
                Label("↑/↓ scroll · Esc close · /resume switches session", dont_extend_height=True),
                body,
            ],
            padding=1,
        ),
        with_background=True,
    )
    return Application(
        layout=Layout(popup_container(dialog), focused_element=body),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=True,
        input=input,
        output=output,
        style=popup_style(style),
    )
