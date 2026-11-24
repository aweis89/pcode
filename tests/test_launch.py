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
from pcode.launch import Choice, Workspace, command, describe, render, step, workspaces


def _session(identity, workspace, updated, project=None):
    return SimpleNamespace(
        id=identity, workspace=str(workspace), project=project and str(project), updated=updated
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
    sessions = [
        _session("a", old, "2026-11-01T00:00:00+00:00"),
        _session("b", new, "2026-11-02T00:00:00+00:00"),
        # A removed worktree's session belongs to its repository.
        _session("c", old / ".worktrees" / "gone", "2026-11-03T00:00:00+00:00", project=old),
        _session("d", tmp_path / "deleted", "2026-11-04T00:00:00+00:00"),
    ]
    rows = workspaces(sessions, [_host("c", old / ".worktrees" / "gone", 5.0)], cwd=home, home=home)
    assert [(row.path, row.sessions, row.running) for row in rows] == [(old, 2, 1), (new, 1, 0)]


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
    assert len(listed) == 3  # height minus the title, blank lines, keys and message
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
    assert "No workspaces yet" in "\n".join(
        render([], 0, width=80, height=8, home=tmp_path, now=0.0)
    )


def test_step():
    rows = [Workspace(Path("/a"), sessions=1), Workspace(Path("/b"))]
    assert step("j", 0, rows) == (1, None)
    assert step("\x1b[B", 1, rows) == (1, None)
    assert step("k", 0, rows) == (0, None)
    assert step("\r", 1, rows) == (1, Choice("new", Path("/b")))
    assert step("c", 0, rows) == (0, Choice("continue", Path("/a")))
    assert step("c", 1, rows) == (1, None)  # nothing to continue
    assert step("s", 0, rows) == (0, None)  # no nested shell unless asked for
    assert step("s", 0, rows, "shell") == (0, Choice("shell", Path("/a")))
    assert step("q", 1, rows) == (1, "quit")
    assert step("o", 0, []) == (0, "open")
    assert step("\r", 0, []) == (0, None)


def test_command(tmp_path):
    def none(_name):
        return None

    new = Choice("new", tmp_path)
    assert command(new, "exit", shell="/bin/zsh", python="py", which=none) == [
        "py",
        "-m",
        "pcode.cli",
    ]
    assert command(Choice("shell", tmp_path), "shell", shell="/bin/zsh", which=none) == [
        "/bin/zsh",
        "-l",
    ]
    assert command(
        Choice("continue", tmp_path), "shell", shell="/bin/zsh", python="py", which=none
    ) == ["/bin/sh", "-c", '"$@"; exec "$0" -l', "/bin/zsh", "py", "-m", "pcode.cli", "-c"]


def test_command_uses_direnv_only_where_an_envrc_applies(tmp_path, monkeypatch):
    def found(name):
        return f"/bin/{name}"

    inner = tmp_path / "root" / "repo"
    inner.mkdir(parents=True)
    # The machine running the tests may have an .envrc above its temp directory.
    with monkeypatch.context() as patched:
        patched.setattr("pcode.launch._has_envrc", lambda _path: False)
        plain = command(Choice("new", inner), "exit", shell="sh", python="py", which=found)
    assert plain == ["py", "-m", "pcode.cli"]
    (tmp_path / "root" / ".envrc").write_text("")
    wrapped = command(
        Choice("new", inner), "exit", shell="sh", python="py", which=found, extra=["--x"]
    )
    assert wrapped == ["direnv", "exec", str(inner), "py", "-m", "pcode.cli", "--x"]
    assert command(Choice("new", inner), "exit", shell="sh", python="py", which=lambda _: None) == [
        "py",
        "-m",
        "pcode.cli",
    ]


@pytest.mark.parametrize("extra", [["hello"], ["-C", "."], ["-c"], ["--attach"], ["-p", "x"]])
def test_launch_refuses_other_session_arguments(monkeypatch, capsys, extra):
    monkeypatch.setattr(sys, "argv", ["pcode", "--launch", *extra])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
    assert "--launch" in capsys.readouterr().err


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


def test_launch_picker_starts_a_shell_in_the_chosen_workspace(tmp_path):
    """The real screen on a pty: move to the second row, ask for a shell there."""
    from pcode.sessions import SavedSession

    home, first, second = _dirs(tmp_path, "home", "first", "second")
    root = tmp_path / "sessions"
    for workspace in (second, first):  # `first` is saved last, so it lists first
        SavedSession.create("test", workspace, root).close()
        time.sleep(0.01)
    shell = tmp_path / "shell"
    shell.write_text('#!/bin/sh\necho "shell in $(pwd) $1"\n')
    shell.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "SHELL": str(shell),
        "PCODE_SESSION_DIR": str(root),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        # Hosts running on this machine would otherwise be listed above these rows.
        "PCODE_HOST_DIR": str(tmp_path / "hosts"),
        "COLUMNS": "100",
        "LINES": "24",
    }
    controller, terminal = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "pcode.cli", "--launch", "shell"],
        stdin=terminal,
        stdout=terminal,
        stderr=terminal,
        cwd=home,
        env=env,
        start_new_session=True,
    )
    os.close(terminal)
    try:
        screen = _read_until(controller, b"q quit").decode(errors="replace")
        assert "first" in screen and "second" in screen and "1 session" in screen
        os.write(controller, b"j")
        _read_until(controller, b"q quit")
        os.write(controller, b"s")
        output = _read_until(controller, b"-l").decode(errors="replace")
        assert f"shell in {second.resolve()} -l" in output
        assert process.wait(timeout=20) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(controller)
