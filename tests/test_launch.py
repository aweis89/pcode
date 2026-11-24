import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pcode.app import main
from pcode.launch import (
    Choice,
    Workspace,
    command,
    describe,
    parse_folder,
    render,
    split_keys,
    step,
    workspaces,
)


def _session(identity, workspace, updated, project=None, turns=1):
    return SimpleNamespace(
        id=identity,
        workspace=str(workspace),
        project=project and str(project),
        updated=updated,
        turns=turns,
    )


def _host(session_id, workspace, updated):
    return SimpleNamespace(session_id=session_id, workspace=str(workspace), updated=updated)


def _dirs(tmp_path, *names):
    paths = [tmp_path / name for name in names]
    for path in paths:
        path.mkdir()
    return paths


def test_workspaces_group_by_project_newest_first(tmp_path):
    home, old, new = _dirs(tmp_path, "home", "old", "new")
    sessions = [  # newest first, as list_sessions returns them
        _session("d", tmp_path / "deleted", "2026-11-04T00:00:00+00:00"),
        # A removed worktree's session belongs to its repository.
        _session("c", old / ".worktrees" / "gone", "2026-11-03T00:00:00+00:00", project=old),
        _session("b", new, "2026-11-02T00:00:00+00:00"),
        _session("a", old, "2026-11-01T00:00:00+00:00"),
        _session("e", new, "2026-11-05T00:00:00+00:00", turns=0),  # opened, never used
    ]
    hosts = [_host("c", old / ".worktrees" / "gone", 5.0), _host("unknown", "", 9.0)]
    rows = workspaces(sessions, hosts, cwd=home, home=home)
    assert [(row.path, row.sessions, row.running, row.latest) for row in rows] == [
        (old, 2, 1, "c"),
        (new, 1, 0, "b"),
    ]


def test_workspaces_drop_scratch_directories_and_keep_home_sessions(tmp_path):
    home, repo, scratch = _dirs(tmp_path, "home", "repo", "scratch")
    (scratch / "repro").mkdir()
    sessions = [
        _session("a", scratch / "repro", "2026-11-03T00:00:00+00:00"),
        _session("b", repo, "2026-11-02T00:00:00+00:00"),
        _session("c", home, "2026-11-01T00:00:00+00:00"),
    ]
    rows = workspaces(sessions, [], cwd=home, home=home, temporary=[scratch])
    assert [row.path for row in rows] == [repo, home]
    assert render(rows, 0, width=80, height=20, home=home, now=0.0)[3].startswith("  ~  ")
    # Started in a scratch directory, it still leads: that is "start here".
    there = workspaces(sessions, [], cwd=scratch / "repro", home=home, temporary=[scratch])
    assert [row.path for row in there] == [scratch / "repro", repo, home]


def test_workspaces_lead_with_cwd_unless_home(tmp_path):
    home, repo, other = _dirs(tmp_path, "home", "repo", "other")
    sessions = [
        _session("a", repo, "2026-11-01T00:00:00+00:00"),
        _session("b", other, "2026-11-02T00:00:00+00:00"),
    ]
    rows = workspaces(sessions, [], cwd=repo, home=home)
    assert [row.path for row in rows] == [repo, other]
    assert rows[0].sessions == 1
    fresh = workspaces([], [], cwd=other, home=home)
    assert fresh == [Workspace(other)]
    assert workspaces([], [], cwd=home, home=home) == []


def test_describe():
    assert describe(Workspace(Path("/x")), 100.0) == "no sessions yet"
    row = Workspace(Path("/x"), sessions=3, running=2, updated=100.0)
    assert describe(row, 100.0 + 7200) == "3 sessions · 2 running · 2h ago"
    assert describe(Workspace(Path("/x"), sessions=1, updated=100.0), 110.0) == (
        "1 session · just now"
    )
    assert describe(Workspace(Path("/x"), running=1, updated=100.0), 110.0) == (
        "1 running · just now"
    )


def test_render_marks_selection_and_scrolls(tmp_path):
    rows = [Workspace(tmp_path / f"repo{index}", sessions=1) for index in range(10)]
    lines = render(rows, 9, width=80, height=8, home=tmp_path, now=0.0, message="oops")
    listed = [line for line in lines if "repo" in line]
    assert len(listed) == 2  # height minus the title, blank lines, keys and message
    assert len(lines) <= 8
    assert listed[-1].startswith("› ~/repo9")
    assert "s shell" not in lines[-2] and lines[-1] == "oops"
    narrow = render(rows, 0, width=20, height=8, home=tmp_path, now=0)
    assert all(len(line) <= 20 for line in narrow)
    assert "s shell" in render(rows, 0, width=80, height=8, home=tmp_path, now=0, then="shell")[-1]
    long = [Workspace(tmp_path / ("deep/" * 30) / "project", sessions=2)]
    (line,) = [
        line
        for line in render(long, 0, width=60, height=8, home=Path("/nowhere"), now=0.0)
        if "project" in line
    ]
    assert "…" in line and line.endswith("project  2 sessions")
    empty = render([], 0, width=80, height=8, home=tmp_path, now=0.0)
    assert "Welcome" in empty[2] and empty[-1] == "enter open folder  q quit"


def test_step():
    rows = [Workspace(Path("/a"), sessions=1), Workspace(Path("/b"))]
    assert step("j", 0, rows) == (1, None)
    assert step("\x1b[B", 1, rows) == (1, None)
    assert step("k", 0, rows) == (0, None)
    assert step("\r", 1, rows) == (1, Choice("new", Path("/b")))
    assert step("c", 0, rows) == (0, None)  # only zero-turn or unknown sessions
    named = [Workspace(Path("/a"), sessions=1, latest="abc")]
    assert step("c", 0, named) == (0, Choice("continue", Path("/a"), "abc"))
    assert step("c", 1, rows) == (1, None)  # nothing to continue
    assert step("s", 0, rows) == (0, None)  # no nested shell unless asked for
    assert step("s", 0, rows, "shell") == (0, Choice("shell", Path("/a")))
    assert step("q", 1, rows) == (1, "quit")
    assert step("o", 0, []) == (0, "open")
    assert step("\r", 0, []) == (0, "open")  # first run: enter is the only way forward
    assert step("s", 0, [], "shell") == (0, "quit")
    assert step("j", 0, []) == (0, None)


def test_split_keys():
    assert split_keys("jj\r") == ["j", "j", "\r"]
    assert split_keys("\x1b[B\x1b[B\x1bOAq") == ["\x1b[B", "\x1b[B", "\x1bOA", "q"]
    assert split_keys("\x1b") == ["\x1b"]
    assert split_keys("\x1b[1;5B") == ["\x1b[1;5B"]  # an unbound key is one key, not five


def test_parse_folder(tmp_path):
    spaced = tmp_path / "My Folder"
    spaced.mkdir()
    assert parse_folder("  ") == (None, "")
    assert parse_folder(str(spaced)) == (spaced.resolve(), "")  # typed with a bare space
    assert parse_folder(str(spaced).replace(" ", "\\ ")) == (spaced.resolve(), "")  # dragged in
    assert parse_folder(f"'{spaced}'") == (spaced.resolve(), "")
    assert parse_folder(str(tmp_path / "missing"))[1].endswith("is not a directory.")


def _none(_name):
    return None


def test_command(tmp_path):
    new = Choice("new", tmp_path)
    plain = ["py", "-m", "pcode.cli"]
    assert command(new, "exit", shell="/bin/zsh", python="py", which=_none, environ={}) == plain
    assert command(
        Choice("shell", tmp_path), "shell", shell="/bin/zsh", which=_none, environ={}
    ) == ["/bin/zsh", "-l"]
    # A continued session is named, and keeps its own model and workspace.
    wrapped = command(
        Choice("continue", tmp_path, "abc"),
        "shell",
        shell="/bin/zsh",
        python="py",
        which=_none,
        environ={},
        always=["--host"],
        fresh=["--model", "m"],
    )
    script = 'trap : INT; "$@"; trap - INT; exec "$0" -l'
    assert wrapped[:4] == ["/bin/sh", "-c", script, "/bin/zsh"]
    assert wrapped[4:] == [*plain, "--host", "--continue", "abc"]
    assert command(
        new, "exit", shell="sh", python="py", which=_none, environ={}, always=["-a"], fresh=["-f"]
    ) == [*plain, "-a", "-f"]


def test_command_uses_direnv_where_an_environment_has_to_change(tmp_path, monkeypatch):
    def found(name):
        return f"/bin/{name}"

    inner = tmp_path / "root" / "repo"
    inner.mkdir(parents=True)
    new = Choice("new", inner)
    plain = ["py", "-m", "pcode.cli"]
    direnv = ["direnv", "exec", str(inner), *plain]
    # The machine running the tests may have an .envrc above its temp directory.
    with monkeypatch.context() as patched:
        patched.setattr("pcode.launch._has_envrc", lambda _path: False)
        assert command(new, "exit", shell="sh", python="py", which=found, environ={}) == plain
        # Another project's variables are loaded here: direnv unloads them.
        loaded = {"DIRENV_DIR": "-/elsewhere"}
        assert command(new, "exit", shell="sh", python="py", which=found, environ=loaded) == direnv
    (tmp_path / "root" / ".envrc").write_text("")
    assert command(new, "exit", shell="sh", python="py", which=found, environ={}) == direnv
    assert command(new, "exit", shell="sh", python="py", which=_none, environ={}) == plain


def test_shell_follows_a_pcode_killed_by_ctrl_c(tmp_path):
    """Ctrl-C reaches the whole foreground group; the wrapper must outlive it."""
    import signal

    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\necho ready\nsleep 30\n")
    shell = tmp_path / "shell"
    shell.write_text("#!/bin/sh\necho shell-follows $1\n")
    for script in (fake, shell):
        script.chmod(0o755)
    argv = command(
        Choice("new", tmp_path), "shell", shell=str(shell), python=str(fake), which=_none
    )
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert process.stdout.readline() == "ready\n"
        os.killpg(process.pid, signal.SIGINT)
        assert process.stdout.read() == "shell-follows -l\n"
        assert process.wait(timeout=20) == 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


@pytest.mark.parametrize(
    "extra",
    [
        ["shell", "hello"],
        ["-C", "."],
        ["-c"],
        ["--attach"],
        ["-p", "x"],
        ["--sessions"],
        ["--hosts"],
    ],
)
def test_launch_refuses_arguments_it_would_drop(monkeypatch, capsys, extra):
    monkeypatch.setattr(sys, "argv", ["pcode", "--launch", *extra])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
    assert "--launch picks" in capsys.readouterr().err


def test_launch_forwards_session_options(monkeypatch, tmp_path):
    seen = {}

    def launch(then, session_dir, *, always, fresh):
        seen.update(then=then, always=always, fresh=fresh)
        return 0

    monkeypatch.setattr("pcode.launch.launch", launch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        sys,
        "argv",
        ["pcode", "--launch", "shell", "--host", "-m", "x:y", "--worktree", "--no-save"]
        + ["--session-dir", str(tmp_path)],
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 0
    assert seen == {
        "then": "shell",
        "always": ["--session-dir", str(tmp_path.resolve()), "--host"],
        "fresh": ["--model", "x:y", "--worktree", "--no-save"],
    }


def _read_until(fd, marker, timeout=20.0):
    data = b""
    deadline = time.monotonic() + timeout
    while marker not in data and time.monotonic() < deadline:
        if select.select([fd], [], [], 0.2)[0]:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
    return data


class _Picker:
    """`pcode --launch shell` on a pty, in a home with two used workspaces."""

    def __init__(self, tmp_path, workspaces=("second", "first")):
        from pcode.sessions import SavedSession

        (self.home,) = _dirs(tmp_path, "home")
        root = tmp_path / "sessions"
        for name in workspaces:  # the last one saved lists first
            saved = SavedSession.create("test", _dirs(tmp_path, name)[0], root)
            saved.info.turns = 1
            saved.save_info()
            saved.close()
            time.sleep(0.01)
        shell = tmp_path / "shell"
        shell.write_text('#!/bin/sh\necho "shell in $(pwd) $1 <done>"\n')
        shell.chmod(0o755)
        env = {
            **os.environ,
            "HOME": str(self.home),
            "SHELL": str(shell),
            "PCODE_SESSION_DIR": str(root),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            # Hosts running on this machine would otherwise be listed above these rows.
            "PCODE_HOST_DIR": str(tmp_path / "hosts"),
            "COLUMNS": "100",
            "LINES": "24",
        }
        # No direnv: an .envrc above the temp directory would be blocked under this HOME.
        env.pop("DIRENV_DIR", None)
        env["PATH"] = os.pathsep.join(
            entry
            for entry in env.get("PATH", "").split(os.pathsep)
            if not (Path(entry) / "direnv").exists()
        )
        self.controller, terminal = pty.openpty()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "pcode.cli", "--launch", "shell"],
            stdin=terminal,
            stdout=terminal,
            stderr=terminal,
            cwd=self.home,
            env=env,
            start_new_session=True,
        )
        os.close(terminal)

    def read(self, marker):
        return _read_until(self.controller, marker).decode(errors="replace")

    def write(self, data):
        os.write(self.controller, data)

    def close(self):
        import signal

        # The picker execs a wrapper that runs pcode as its child: kill the
        # whole session, or that pcode outlives the test on a dead pty.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            pass  # macOS answers EPERM, not ESRCH, once the leader is a zombie
        self.process.wait()
        os.close(self.controller)


@pytest.fixture
def picker(tmp_path):
    running = []

    def start(**options):
        running.append(_Picker(tmp_path, **options))
        return running[-1]

    yield start
    for one in running:
        one.close()


def test_launch_picker_starts_a_shell_in_the_chosen_workspace(picker, tmp_path):
    """The real screen: move to the second row, ask for a shell there."""
    screen = picker()
    shown = screen.read(b"q quit")
    assert "first" in shown and "second" in shown and "1 session" in shown
    # An arrow key split across two writes is still an arrow, not Esc (quit).
    screen.write(b"\x1b")
    screen.write(b"[B")
    screen.read(b"q quit")
    screen.write(b"s")
    assert f"shell in {(tmp_path / 'second').resolve()} -l <done>" in screen.read(b"<done>")
    assert screen.process.wait(timeout=20) == 0


def test_launch_picker_takes_keys_that_arrive_together(picker, tmp_path):
    screen = picker(workspaces=("third", "second", "first"))
    screen.read(b"q quit")
    screen.write(b"jjs")
    assert f"shell in {(tmp_path / 'third').resolve()} -l <done>" in screen.read(b"<done>")


def test_launch_picker_quit_leaves_a_shell_when_one_was_promised(picker):
    screen = picker()
    screen.read(b"q quit")
    screen.write(b"q")
    assert f"shell in {screen.home.resolve()} -l <done>" in screen.read(b"<done>")


def test_launch_picker_first_run_opens_a_folder(picker, tmp_path):
    screen = picker(workspaces=())
    assert "Welcome" in screen.read(b"q quit")
    (target,) = _dirs(tmp_path, "new project")
    screen.write(b"\r")
    screen.read(b"Folder")
    screen.write(str(target).replace(" ", "\\ ").encode() + b"\n")
    # The folder starts pcode, which is the offline preview with no model set.
    assert "pcode" in screen.read(b"UI preview")
