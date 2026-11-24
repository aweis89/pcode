"""`pcode --launch`: pick a workspace, then become pcode there.

A terminal opened from the Dock starts in the home directory, where pcode has
no project. This screen lists the workspaces that have saved sessions, and the
choice replaces this process with a fresh `pcode` in that directory, so nothing
of the picker (its imports, its project root) carries into the session.
"""

import os
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pcode.host_ui import age

# What follows when the started pcode quits: back to whatever ran `--launch`,
# or a login shell in the workspace (a terminal app's window must not close).
THEN = ("exit", "shell")
Kind = Literal["new", "continue", "shell"]

_UP = {"\x1b[A", "\x1bOA", "k", "\x10"}
_DOWN = {"\x1b[B", "\x1bOB", "j", "\x0e"}
_QUIT = {"q", "\x1b", "\x03", "\x04"}


@dataclass(frozen=True)
class Workspace:
    path: Path
    sessions: int = 0
    running: int = 0
    updated: float = 0.0


@dataclass(frozen=True)
class Choice:
    kind: Kind
    path: Path


def _timestamp(text: str) -> float:
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def workspaces(sessions: Sequence, hosts: Sequence, *, cwd: Path, home: Path) -> list[Workspace]:
    """Projects with saved sessions, most recently used first, after `cwd`.

    A session in a linked worktree counts toward its repository, since the
    worktree is usually gone and a new session there starts from the project.
    Directories that no longer exist are dropped. `cwd` leads so the screen
    also works as "start here", except at `home`, where nobody means to.
    """
    found: dict[Path, Workspace] = {}
    project_of: dict[str, Path] = {}
    for info in sessions:
        path = Path(info.project or info.workspace)
        project_of[info.id] = path
        seen = found.get(path, Workspace(path))
        found[path] = Workspace(
            path, seen.sessions + 1, seen.running, max(seen.updated, _timestamp(info.updated))
        )
    for entry in hosts:
        path = project_of.get(entry.session_id) or Path(entry.workspace)
        seen = found.get(path, Workspace(path))
        found[path] = Workspace(
            path, seen.sessions, seen.running + 1, max(seen.updated, entry.updated)
        )
    rows = sorted(
        (row for row in found.values() if row.path != cwd and row.path.is_dir()),
        key=lambda row: row.updated,
        reverse=True,
    )
    if cwd != home:
        rows.insert(0, found.get(cwd, Workspace(cwd)))
    return rows


def short(path: Path, home: Path) -> str:
    try:
        return str(Path("~") / path.relative_to(home))
    except ValueError:
        return str(path)


def describe(row: Workspace, now: float) -> str:
    if not row.sessions and not row.running:
        return "no sessions yet"
    parts = []
    if row.sessions:
        parts.append(f"{row.sessions} session{'' if row.sessions == 1 else 's'}")
    if row.running:
        parts.append(f"{row.running} running")
    if row.updated:
        parts.append(age(max(0.0, now - row.updated)))
    return " · ".join(parts)


def _cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def render(
    rows: Sequence[Workspace],
    selected: int,
    *,
    width: int,
    height: int,
    home: Path,
    now: float,
    then: str = "exit",
    message: str = "",
) -> list[str]:
    """The screen as plain lines; the selected row is marked with `›`."""
    keys = ["enter new session", "c continue", "o open folder"]
    if then == "shell":
        keys.append("s shell")
    keys.append("q quit")
    lines = ["pcode", ""]
    if not rows:
        lines.append("  No workspaces yet. Press o to open a folder.")
    # A path's tail names the project, so a long one loses its head, and never
    # so much of the line that the session counts are pushed off it.
    limit = max(12, width - 40)
    names = [
        name if len(name) <= limit else "…" + name[1 - limit :]
        for name in (short(row.path, home) for row in rows)
    ]
    column = max((len(name) for name in names), default=0)
    # Keep the selection in view: the list scrolls in a window of `room` rows.
    room = max(1, height - 5)
    first = min(max(0, selected - room + 1), max(0, len(rows) - room))
    for index in range(first, min(len(rows), first + room)):
        mark = "›" if index == selected else " "
        line = f"{mark} {names[index]:<{column}}  {describe(rows[index], now)}"
        lines.append(_cut(line, width))
    lines += ["", _cut("  ".join(keys), width)]
    if message:
        lines.append(_cut(message, width))
    return lines


def step(key: str, selected: int, rows: Sequence[Workspace], then: str = "exit"):
    """One keypress: the new selection, and a Choice, "open", "quit" or None."""
    if key in _QUIT:
        return selected, "quit"
    if key == "o":
        return selected, "open"
    if not rows:
        return selected, None
    if key in _UP:
        return max(0, selected - 1), None
    if key in _DOWN:
        return min(len(rows) - 1, selected + 1), None
    row = rows[selected]
    if key in ("\r", "\n"):
        return selected, Choice("new", row.path)
    if key == "c" and row.sessions:
        return selected, Choice("continue", row.path)
    if key == "s" and then == "shell":
        return selected, Choice("shell", row.path)
    return selected, None


def _has_envrc(path: Path) -> bool:
    return any((directory / ".envrc").is_file() for directory in (path, *path.parents))


def command(
    choice: Choice,
    then: str,
    *,
    shell: str,
    extra: Sequence[str] = (),
    python: str = sys.executable,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """The argv that replaces this process, run from `choice.path`.

    pcode goes through `direnv exec` when an `.envrc` applies: direnv's shell
    hook fires at a prompt, which a terminal's launch command never shows, so
    the workspace's variables would otherwise be silently missing.
    """
    login = [shell, "-l"]
    if choice.kind == "shell":
        return login
    run = [python, "-m", "pcode.cli", *extra]
    if choice.kind == "continue":
        run.append("-c")
    if which("direnv") and _has_envrc(choice.path):
        run = ["direnv", "exec", str(choice.path), *run]
    if then != "shell":
        return run
    # `$0` is the login shell: whatever pcode's exit status, the pane stays open.
    return ["/bin/sh", "-c", '"$@"; exec "$0" -l', shell, *run]


def read_key(fd: int) -> str:
    data = os.read(fd, 32)
    return data.decode(errors="replace")


def _draw(lines: Sequence[str]) -> None:
    out = ["\x1b[H\x1b[2J"]
    for index, line in enumerate(lines):
        if index == 0:
            line = f"\x1b[1m{line}\x1b[m"
        elif line.startswith("›"):
            line = f"\x1b[7m{line}\x1b[m"
        out.append(line + "\r\n")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def _ask_folder(restore: Callable[[], None], resume: Callable[[], None]) -> tuple[Path | None, str]:
    """Read a path on a normal line; (folder, "") or (None, why not)."""
    restore()
    try:
        text = input("Folder: ").strip()
    except EOFError, KeyboardInterrupt:
        text = ""
    finally:
        resume()
    if not text:
        return None, ""
    path = Path(text).expanduser()
    if not path.is_dir():
        return None, f"{text} is not a directory."
    return path.resolve(), ""


def pick(rows: list[Workspace], then: str, *, home: Path) -> Choice | None:
    """Run the screen on the terminal until something is chosen; None for quit."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)

    def restore() -> None:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    def resume() -> None:
        tty.setraw(fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")

    selected, message = 0, ""
    resume()
    try:
        while True:
            size = shutil.get_terminal_size()
            _draw(
                render(
                    rows,
                    selected,
                    width=size.columns,
                    height=size.lines,
                    home=home,
                    now=time.time(),
                    then=then,
                    message=message,
                )
            )
            selected, action = step(read_key(fd), selected, rows, then)
            message = ""
            if action == "quit":
                return None
            if action == "open":
                folder, message = _ask_folder(restore, resume)
                if folder is not None:
                    return Choice("new", folder)
            elif action is not None:
                return action
    finally:
        restore()


def launch(then: str, session_dir: Path | None = None) -> int:
    """Show the picker and replace this process with the choice; 0 when quit."""
    from pcode.cli import restore_stdin
    from pcode.host_protocol import list_hosts
    from pcode.sessions import list_sessions

    # The entry point silenced echo for typeahead; the picker and whatever it
    # starts both need the terminal as it was, and exec skips every cleanup.
    restore_stdin()
    home = Path.home()
    rows = workspaces(list_sessions(session_dir), list_hosts(), cwd=Path.cwd(), home=home)
    choice = pick(rows, then, home=home)
    if choice is None:
        return 0
    extra = ["--session-dir", str(session_dir.resolve())] if session_dir else []
    argv = command(choice, then, shell=os.environ.get("SHELL") or "/bin/sh", extra=extra)
    try:
        os.chdir(choice.path)
    except OSError as error:
        print(f"pcode: cannot enter {choice.path}: {error.strerror}", file=sys.stderr)
        return 2
    os.environ["PWD"] = str(choice.path)  # getcwd is right; an inherited PWD is not
    sys.stdout.flush()
    os.execvp(argv[0], argv)
