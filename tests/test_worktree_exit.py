"""Leaving a session worktree: untouched ones vanish, unmerged ones ask, work is never lost."""

import asyncio
import io
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel

from pcode import worktree
from pcode.app import PreviewApp, _leave_worktree_on_exit
from pcode.live import AgentRuntime
from pcode.preferences import save_preferences
from pcode.sessions import SavedSession, SessionError


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


def saved_session_in(path: Path, root: Path) -> str:
    """A closed session with one turn, recorded as working in `path`."""

    async def model(messages, info):
        yield "Saved answer"

    saved = SavedSession.create("test:local", path, root)
    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)), saved)

    async def turn():
        _ = [event async for event in runtime.stream("First question")]

    asyncio.run(turn())
    runtime.close()
    return saved.info.id


def test_resume_switches_to_a_sibling_worktree_and_tidies_the_old_one(repo, tmp_path):
    root = tmp_path / "sessions"
    other = worktree.create(repo, "pcode-other")
    commit(other.path, "f.txt")  # unmerged work, so its session is worth resuming
    (other.path / ".agents" / "skills" / "demo").mkdir(parents=True)
    (other.path / ".agents" / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\n---\nbody\n"
    )
    identity = saved_session_in(other.path, root)
    created, session, app = make(repo, tmp_path)  # untouched pcode-abc, empty session
    app.session_dir = root
    notes = []
    app.transcript.note = lambda text, **_: notes.append(text)
    with patch("pcode.agent.create_agent", return_value=Agent("test")) as create:
        asyncio.run(app.resume_session(identity))
    try:
        assert create.call_args.args[1] == other.path
        assert app.workspace == other.path
        assert app.runtime.session.info.id == identity
        assert app.runtime.turns == 1
        assert app.registry.find("/skill:demo") is not None
        # The untouched worktree left behind is gone, with its empty session.
        assert not created.path.exists()
        assert git(repo, "branch", "--list", "pcode-abc") == ""
        assert not session.directory.exists()
        assert any("removed untouched" in note for note in notes)
        assert any(str(other.path) in note for note in notes)
    finally:
        app.runtime.close()


def test_resume_leaves_unmerged_worktree_with_a_note(repo, tmp_path):
    root = tmp_path / "sessions"
    identity = saved_session_in(repo, root)  # a mainline session
    created, session, app = make(repo, tmp_path, turns=1)
    commit(created.path, "f.txt")
    app.session_dir = root
    notes = []
    app.transcript.note = lambda text, **_: notes.append(text)
    with patch("pcode.agent.create_agent", return_value=Agent("test")):
        asyncio.run(app.resume_session(identity))
    try:
        assert app.workspace == repo
        assert created.path.exists()
        assert not (repo / "f.txt").exists()
        assert any("1 unmerged commit" in note and "resumes there" in note for note in notes)
    finally:
        app.runtime.close()
        session.close()


def test_resume_refuses_another_repository_or_a_missing_directory(repo, tmp_path):
    root = tmp_path / "sessions"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    foreign = saved_session_in(elsewhere, root)
    gone = saved_session_in(tmp_path / "gone", root)
    created, session, app = make(repo, tmp_path)
    app.session_dir = root
    original = app.runtime
    with pytest.raises(SessionError, match="cross-repo"):
        asyncio.run(app.resume_session(foreign))
    with pytest.raises(SessionError, match="no longer exists"):
        asyncio.run(app.resume_session(gone))
    assert app.runtime is original
    assert app.workspace == created.path and created.path.exists()
    session.close()
