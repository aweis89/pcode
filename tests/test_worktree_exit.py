"""Leaving a session worktree: untouched ones vanish, unmerged ones ask, work is never lost."""

import io
import subprocess
from pathlib import Path

import pytest

from pcode import worktree
from pcode.app import PreviewApp, _leave_worktree_on_exit
from pcode.preferences import save_preferences
from pcode.sessions import SavedSession


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = (tmp_path / "repo").resolve()
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "README").write_text("hello\n")
    git(path, "add", "README")
    git(path, "commit", "-q", "-m", "init")
    return path


def commit(path: Path, name: str, text: str = "x\n") -> None:
    (path / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-q", "-m", f"add {name}")


class FakeRuntime:
    def __init__(self, session):
        self.session = session


def make(repo, tmp_path, name="pcode-abc", turns=0):
    created = worktree.create(repo, name)
    session = SavedSession.create("test:local", created.path, tmp_path / "sessions")
    session.info.turns = turns
    session.save_info()
    app = PreviewApp(workspace=created.path)
    app.runtime = FakeRuntime(session)
    return created, session, app


def leave(app, answer=None, **kw):
    err = io.StringIO()
    ask = None if answer is None else (lambda _: answer)
    _leave_worktree_on_exit(app, ask=ask, stream=err, **kw)
    return err.getvalue()


def test_untouched_worktree_and_empty_session_are_deleted(repo, tmp_path):
    created, session, app = make(repo, tmp_path)
    out = leave(app)
    assert "removed untouched" in out
    assert not created.path.exists()
    assert git(repo, "branch", "--list", "pcode-abc") == ""
    assert not session.directory.exists()
    assert app.runtime.session is None


def test_untouched_worktree_with_turns_keeps_session_and_repoints(repo, tmp_path):
    created, session, app = make(repo, tmp_path, turns=2)
    leave(app)
    assert not created.path.exists()
    assert session.directory.exists()
    assert session.info.workspace == str(repo)
    session.close()
    reopened = SavedSession.open(session.info.id, tmp_path / "sessions")
    assert reopened.info.workspace == str(repo)
    reopened.close()


def test_ignored_files_do_not_count_as_touched(repo, tmp_path):
    (repo / ".gitignore").write_text(".venv/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    created, session, app = make(repo, tmp_path)
    (created.path / ".venv").mkdir()
    (created.path / ".venv" / "bin").write_text("")
    assert worktree.is_untouched(created)
    leave(app)
    assert not created.path.exists()


def test_untracked_or_dirty_worktree_is_kept(repo, tmp_path):
    created, session, app = make(repo, tmp_path)
    (created.path / "scratch").write_text("x")
    assert not worktree.is_untouched(created)
    out = leave(app, answer="y")
    assert created.path.exists()
    assert "uncommitted changes" not in out  # untracked only: not dirty, not untouched
    assert "resumes there" in out
    (created.path / "README").write_text("edit\n")
    out = leave(app, answer="y")
    assert "uncommitted changes" in out
    assert created.path.exists()
    session.close()


def test_hand_made_worktree_is_never_removed(repo, tmp_path):
    created, session, app = make(repo, tmp_path, name="mine")
    out = leave(app, answer="y")
    assert created.path.exists()
    assert "resumes there" in out
    commit(created.path, "f.txt")
    out = leave(app, answer="y")
    assert created.path.exists()
    assert "1 unmerged" in out
    session.close()


@pytest.mark.parametrize("answer", ["", "y", "Yes"])
def test_unmerged_commits_ask_and_merge_by_default(repo, tmp_path, answer):
    created, session, app = make(repo, tmp_path, turns=1)
    commit(created.path, "f.txt")
    out = leave(app, answer=answer)
    assert "not in main" in out
    assert "merged pcode-abc into main" in out
    assert (repo / "f.txt").exists()
    assert not created.path.exists()
    assert git(repo, "branch", "--list", "pcode-abc") == ""
    assert session.info.workspace == str(repo)
    session.close()


def test_declining_keeps_everything(repo, tmp_path):
    created, session, app = make(repo, tmp_path, turns=1)
    commit(created.path, "f.txt")
    out = leave(app, answer="n")
    assert "kept" in out and "resumes there" in out
    assert created.path.exists()
    assert not (repo / "f.txt").exists()
    assert session.info.workspace == str(created.path)
    session.close()


def test_nobody_to_ask_keeps(repo, tmp_path):
    created, session, app = make(repo, tmp_path, turns=1)
    commit(created.path, "f.txt")
    out = leave(app)  # --print
    assert "1 unmerged" in out and created.path.exists()
    session.close()


def test_worktree_exit_setting(repo, tmp_path):
    save_preferences(worktree_exit="keep")
    created, session, app = make(repo, tmp_path, turns=1)
    commit(created.path, "f.txt")
    out = leave(app, answer="y")
    assert "unmerged" in out and created.path.exists()
    save_preferences(worktree_exit="merge")
    out = leave(app)  # no prompt needed
    assert "merged pcode-abc into main" in out and not created.path.exists()
    session.close()


def test_refused_merge_keeps_worktree_and_session(repo, tmp_path):
    created, session, app = make(repo, tmp_path, turns=1)
    commit(created.path, "README", "theirs\n")
    commit(repo, "README", "ours\n")
    out = leave(app, answer="y")
    assert "conflicts" in out and "kept" in out
    assert created.path.exists()
    assert session.info.workspace == str(created.path)
    assert git(repo, "status", "--porcelain") == ""
    session.close()


def test_finish_command_merges_removes_and_quits(repo, tmp_path):
    created, session, app = make(repo, tmp_path, turns=1)
    notes = []
    app.transcript.note = lambda text, **_: notes.append(text)
    commit(created.path, "f.txt")
    app.worktree("finish")
    assert "merged pcode-abc into main; removed" in notes[-1]
    assert not created.path.exists()
    assert app.running is False
    assert session.info.workspace == str(repo)
    session.close()


def test_finish_command_refusal_changes_nothing(repo, tmp_path):
    created, session, app = make(repo, tmp_path, turns=1)
    (created.path / "README").write_text("dirty\n")
    with pytest.raises(ValueError, match="uncommitted"):
        app.worktree("finish")
    assert created.path.exists() and app.running
    session.close()
