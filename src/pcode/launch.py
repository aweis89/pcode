"""`pcode --launch`: pick a workspace, then become pcode there.

A terminal opened from the Dock starts in the home directory, where pcode has
no project. This screen lists the workspaces that have saved sessions, and the
choice replaces this process with a fresh `pcode` in that directory, so nothing
of the picker (its imports, its project root) carries into the session.
"""

import os
import re
import select
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
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
# One key per match: a CSI or SS3 sequence, else a single character.
_KEY = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1bO.|.", re.DOTALL)
# How long a lone Esc waits for the rest of an arrow key split across reads.
_ESCAPE_WAIT = 0.05


@dataclass(frozen=True)
class Workspace:
    path: Path
    sessions: int = 0
    running: int = 0
    updated: float = 0.0
    # The newest session's ID: what `c` continues, wherever in the project it ran.
    latest: str = ""


@dataclass(frozen=True)
class Choice:
    kind: Kind
    path: Path
    session: str = ""


def _timestamp(text: str) -> float:
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def temporary_roots() -> list[Path]:
    """Where scratch directories live; a session there is not a project to return to."""
    roots = {Path("/tmp"), Path("/var/tmp"), Path("/var/folders"), Path(tempfile.gettempdir())}
    return sorted({root.resolve() for root in roots} | roots)


def workspaces(
    sessions: Sequence,
    hosts: Sequence,
    *,
    cwd: Path,
    home: Path,
    temporary: Sequence[Path] = (),
) -> list[Workspace]:
    """Projects with saved sessions, most recently used first, after `cwd`.

    A session in a linked worktree counts toward its repository, since the
    worktree is usually gone and a new session there starts from the project.
    Sessions that never had a turn, directories that no longer exist, and
    anything under a `temporary` root are dropped. `cwd` leads so the screen
    also works as "start here", except at `home`, which is listed only for the
    sessions it has.
    """
    found: dict[Path, Workspace] = {}
    project_of: dict[str, Path] = {}
    for info in sessions:  # newest first, so a row's first session is its latest
        path = Path(info.project or info.workspace)
        project_of[info.id] = path
        if not info.turns:
            continue
        seen = found.get(path, Workspace(path))
        found[path] = Workspace(
            path,
            seen.sessions + 1,
            seen.running,
            max(seen.updated, _timestamp(info.updated)),
            seen.latest or info.id,
        )
    for entry in hosts:
        if entry.session_id not in project_of and not entry.workspace:
            continue
        path = project_of.get(entry.session_id) or Path(entry.workspace)
        seen = found.get(path, Workspace(path))
        found[path] = Workspace(
            path, seen.sessions, seen.running + 1, max(seen.updated, entry.updated), seen.latest
        )
    lead = cwd != home
    rows = sorted(
        (
            row
            for row in found.values()
            if not (lead and row.path == cwd)
            and row.path.is_dir()
            and not any(row.path.is_relative_to(root) for root in temporary)
        ),
        key=lambda row: row.updated,
        reverse=True,
    )
    if lead:
        rows.insert(0, found.get(cwd, Workspace(cwd)))
    return rows


def short(path: Path, home: Path) -> str:
    if path == home:
        return "~"
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
    keys = ["enter new session", "c continue", "o open folder"] if rows else ["enter open folder"]
    if then == "shell":
        keys.append("s shell")
    keys.append("q quit")
    lines = ["pcode", ""]
    if not rows:
        lines += [
            "  Welcome. pcode works inside a project folder: it reads, edits and",
            "  runs things there. Press enter to choose one.",
        ]
        lines = [_cut(line, width) for line in lines]
    # A path's tail names the project, so a long one loses its head, and never
    # so much of the line that the session counts are pushed off it.
    limit = max(12, width - 40)
    names = [
        name if len(name) <= limit else "…" + name[1 - limit :]
        for name in (short(row.path, home) for row in rows)
    ]
    column = max((len(name) for name in names), default=0)
    # Keep the selection in view: the list scrolls in a window of `room` rows,
    # which leaves the title, two blank lines, the keys and a message line.
    room = max(1, height - 6)
    first = min(max(0, selected - room + 1), max(0, len(rows) - room))
    for index in range(first, min(len(rows), first + room)):
        mark = "›" if index == selected else " "
        line = f"{mark} {names[index]:<{column}}  {describe(rows[index], now)}"
        lines.append(_cut(line, width))
    lines += ["", _cut("  ".join(keys), width)]
    if message:
        lines.append(_cut(message, width))
    return lines


def split_keys(data: str) -> list[str]:
    """Keys from one read, which can hold several (typeahead, auto-repeat)."""
    return _KEY.findall(data)


def step(key: str, selected: int, rows: Sequence[Workspace], then: str = "exit"):
    """One keypress: the new selection, and a Choice, "open", "quit" or None."""
    if key in _QUIT:
        return selected, "quit"
    if key == "o":
        return selected, "open"
    if not rows:
        if key in ("\r", "\n"):
            return selected, "open"
        if key == "s" and then == "shell":
            return selected, "quit"  # which in this mode is a shell right here
        return selected, None
    if key in _UP:
        return max(0, selected - 1), None
    if key in _DOWN:
        return min(len(rows) - 1, selected + 1), None
    row = rows[selected]
    if key in ("\r", "\n"):
        return selected, Choice("new", row.path)
    if key == "c" and row.latest:
        return selected, Choice("continue", row.path, row.latest)
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
    always: Sequence[str] = (),
    fresh: Sequence[str] = (),
    python: str = sys.executable,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] = os.environ,
) -> list[str]:
    """The argv that replaces this process, run from `choice.path`.

    `always` goes to any pcode started and `fresh` only to a new session. A
    continued session is named by ID, since "this directory's latest" misses
    one that ran in a worktree or a subdirectory of the project.

    pcode goes through `direnv exec` when an `.envrc` applies: direnv's shell
    hook fires at a prompt, which a terminal's launch command never shows, so
    the workspace's variables would otherwise be silently missing. Likewise
    when this process already carries another directory's variables
    (`DIRENV_DIR`), which `direnv exec` unloads.
    """
    login = [shell, "-l"]
    if choice.kind == "shell":
        return login
    run = [python, "-m", "pcode.cli", *always]
    run += ["--continue", choice.session] if choice.kind == "continue" else fresh
    if which("direnv") and (_has_envrc(choice.path) or environ.get("DIRENV_DIR")):
        run = ["direnv", "exec", str(choice.path), *run]
    if then != "shell":
        return run
    # `$0` is the login shell, so the pane stays open however pcode ends. The
    # trap keeps a Ctrl-C that kills pcode from taking this wrapper with it;
    # a trapped (not ignored) signal is back to its default in pcode itself.
    return ["/bin/sh", "-c", 'trap : INT; "$@"; trap - INT; exec "$0" -l', shell, *run]


def read_keys(fd: int) -> list[str]:
    data = os.read(fd, 64)
    if not data:
        return ["q"]  # the terminal went away
    if data.endswith(b"\x1b") and select.select([fd], [], [], _ESCAPE_WAIT)[0]:
        data += os.read(fd, 64)
    return split_keys(data.decode(errors="replace"))


def _draw(lines: Sequence[str]) -> None:
    out = []
    for index, line in enumerate(lines):
        if index == 0:
            line = f"\x1b[1m{line}\x1b[m"
        elif line.startswith("›"):
            line = f"\x1b[7m{line}\x1b[m"
        out.append(line)
    # No newline after the last line: a full screen would scroll the title off.
    sys.stdout.write("\x1b[H\x1b[2J" + "\r\n".join(out))
    sys.stdout.flush()


def parse_folder(text: str) -> tuple[Path | None, str]:
    """A typed or dragged-in path as (folder, "") or (None, why not)."""
    text = text.strip()
    if not text:
        return None, ""
    try:
        # Finder drops a path shell-quoted (`My\ Folder`); a typed one may
        # hold bare spaces, which is then several words and taken as written.
        words = shlex.split(text)
    except ValueError:
        words = []
    path = Path(words[0] if len(words) == 1 else text).expanduser()
    if not path.is_dir():
        return None, f"{text} is not a directory."
    return path.resolve(), ""


def _complete_paths() -> None:
    """Tab completes directories at the folder prompt, where readline exists."""
    try:
        import readline
    except ImportError:
        return

    def complete(text: str, state: int) -> str | None:
        base = Path(text).expanduser()
        parent, stem = (base, "") if text.endswith("/") or not text else (base.parent, base.name)
        try:
            names = sorted(
                entry.name
                for entry in parent.iterdir()
                if entry.name.startswith(stem) and entry.is_dir()
            )
        except OSError:
            return None
        head = text[: len(text) - len(stem)]
        return f"{head}{names[state]}/" if state < len(names) else None

    readline.set_completer(complete)
    readline.set_completer_delims("")
    # macOS ships libedit under the readline name, with its own binding syntax.
    libedit = "libedit" in (readline.__doc__ or "")
    readline.parse_and_bind("bind ^I rl_complete" if libedit else "tab: complete")


def _ask_folder(restore: Callable[[], None], resume: Callable[[], None]) -> tuple[Path | None, str]:
    """Read a path on a normal line, outside the picker's screen."""
    restore()
    try:
        _complete_paths()
        text = input("Folder (tab completes, empty to go back): ")
    except EOFError, KeyboardInterrupt:
        text = ""
    finally:
        resume()
    return parse_folder(text)


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
            message = ""
            for key in read_keys(fd):
                selected, action = step(key, selected, rows, then)
                if action == "quit":
                    return None
                if action == "open":
                    folder, message = _ask_folder(restore, resume)
                    if folder is not None:
                        return Choice("new", folder)
                    break  # keys typed before the prompt are not for the list
                if action is not None:
                    return action
    finally:
        restore()


def _login_shell() -> str:
    shell = os.environ.get("SHELL", "")
    return shell if shell and os.access(shell, os.X_OK) else "/bin/sh"


def launch(
    then: str,
    session_dir: Path | None = None,
    *,
    always: Sequence[str] = (),
    fresh: Sequence[str] = (),
) -> int:
    """Show the picker and replace this process with the choice.

    Quitting returns 0, except that `then="shell"` promised a shell: the
    window this is the launch command of would otherwise close.
    """
    from pcode.cli import restore_stdin
    from pcode.host_protocol import list_hosts
    from pcode.sessions import list_sessions

    # The entry point silenced echo for typeahead; the picker and whatever it
    # starts both need the terminal as it was, and exec skips every cleanup.
    restore_stdin()
    home = Path.home()
    try:
        cwd = Path.cwd()
    except OSError:  # started in a directory that has since been deleted
        cwd = home
    # Scratch directories are noise, unless that is where this was started.
    temporary = temporary_roots()
    if any(cwd.is_relative_to(root) for root in temporary):
        temporary = []
    rows = workspaces(
        list_sessions(session_dir), list_hosts(), cwd=cwd, home=home, temporary=temporary
    )
    choice = pick(rows, then, home=home)
    if choice is None:
        if then != "shell":
            return 0
        choice = Choice("shell", cwd)
    argv = command(choice, then, shell=_login_shell(), always=always, fresh=fresh)
    try:
        os.chdir(choice.path)
        os.environ["PWD"] = str(choice.path)  # getcwd is right; an inherited PWD is not
        sys.stdout.flush()
        os.execvp(argv[0], argv)
    except OSError as error:
        print(f"pcode: cannot start in {choice.path}: {error.strerror}", file=sys.stderr)
        return 2
