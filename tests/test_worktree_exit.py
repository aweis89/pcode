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


def test_worktree_is_kept_while_another_session_is_open_in_it(repo, tmp_path):
    """A copied session shares its original's worktree; leaving one must not remove it."""
    created, session, app = make(repo, tmp_path)
    other = SavedSession.create("test:local", created.path, tmp_path / "sessions")
    out = leave(app)
    assert f"session {other.info.id} is still open" in out
    assert created.path.exists()
    assert session.directory.exists()
    other.close()
    assert "removed untouched" in leave(app)
    assert not created.path.exists()


def test_worktree_is_kept_for_a_closed_session_that_can_be_resumed_there(repo, tmp_path):
    """Leaving a `--fork` copy keeps the worktree its original will resume in."""
    created, session, app = make(repo, tmp_path)
    other = SavedSession.create("test:local", created.path, tmp_path / "sessions")
    other.info.turns = 1
    other.save_info()
    other.close()
    out = leave(app)
    assert f"session {other.info.id} still works in" in out
    assert created.path.exists()


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
    notes = []
    # A host stopped for being idle: nobody chose to leave, so nothing merges.
    worktree.leave_worktree(created.path, session, ask=None, notify=notes.append, merge=False)
    assert "unmerged" in notes[-1] and created.path.exists()
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
    app.controller.worktree("finish")
    assert notes[-1] == "✓ merged pcode-abc into main and removed its worktree"
    assert not created.path.exists()
    assert app.running is False
    assert session.info.workspace == str(repo)
    session.close()


def test_finish_command_refusal_changes_nothing(repo, tmp_path):
    created, session, app = make(repo, tmp_path, turns=1)
    (created.path / "README").write_text("dirty\n")
    with pytest.raises(ValueError, match="uncommitted"):
        app.controller.worktree("finish")
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
        asyncio.run(app.controller.resume_session(identity))
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
        asyncio.run(app.controller.resume_session(identity))
    try:
        assert app.workspace == repo
        assert created.path.exists()
        assert not (repo / "f.txt").exists()
        assert any("1 unmerged commit" in note and "resumes there" in note for note in notes)
    finally:
        app.runtime.close()
        session.close()


def test_resume_continues_here_when_the_sessions_worktree_was_removed(repo, tmp_path):
    root = tmp_path / "sessions"
    removed = worktree.create(repo, "hand-removed")  # Not pcode's to recreate.
    identity = saved_session_in(removed.path, root)
    worktree.remove(removed, force=True)
    created, session, app = make(repo, tmp_path)
    app.session_dir = root
    notes = []
    app.transcript.note = lambda text, **_: notes.append(text)
    with patch("pcode.agent.create_agent", return_value=Agent("test")):
        asyncio.run(app.controller.resume_session(identity))
    try:
        assert app.workspace == created.path
        assert any("no longer exists" in note for note in notes)
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
        asyncio.run(app.controller.resume_session(foreign))
    with pytest.raises(SessionError, match="no longer exists"):
        asyncio.run(app.controller.resume_session(gone))
    assert app.runtime is original
    assert app.workspace == created.path and created.path.exists()
    session.close()


def test_worktree_created_by_another_session_is_never_tidied(repo, tmp_path):
    """A process merely started inside another session's worktree leaves it alone.

    Its own session root can't see the creator (`--session-dir`, `--no-save`),
    so only the recorded owner protects a live session from losing its directory.
    """
    created = worktree.create(repo, "pcode-abc")
    worktree.claim(created, "creator-session-id")
    session = SavedSession.create("test:local", created.path, tmp_path / "elsewhere")
    app = PreviewApp(workspace=created.path)
    app.runtime = FakeRuntime(session)
    out = leave(app)
    assert "session creator- created it" in out
    assert created.path.exists()
    app.runtime = FakeRuntime(None)
    leave(app)
    assert created.path.exists()


def test_owner_tidies_its_worktree_without_a_saved_session(repo, tmp_path):
    """`--no-save`, or a session swapped by `/new`: the creating ID still owns it."""
    created = worktree.create(repo, "pcode-abc")
    worktree.claim(created, "creator-session-id")
    app = PreviewApp(workspace=created.path)
    app.runtime = FakeRuntime(None)
    assert "removed untouched" in leave(app, owner="creator-session-id")
    assert not created.path.exists()


def test_owner_without_a_session_keeps_a_worktree_its_fork_works_in(repo, tmp_path):
    """A host stopped before its runtime loaded has no session, only the owner ID.

    A fork of that session (made because the original was busy) shares the
    worktree, so the owner's exit must still ask who else works there.
    """
    root = tmp_path / "sessions"
    created = worktree.create(repo, "pcode-abc")
    worktree.claim(created, "creator-session-id")
    fork = SavedSession.create("test:local", created.path, root)
    app = PreviewApp(workspace=created.path, session_dir=root)
    app.runtime = FakeRuntime(None)
    out = leave(app, owner="creator-session-id")
    assert f"session {fork.info.id} is still open in" in out
    assert created.path.exists()
    fork.close()


def test_owner_tidies_its_worktree_after_new(repo, tmp_path):
    created, session, app = make(repo, tmp_path)
    worktree.claim(created, "creator-session-id")  # `session` came later, from /new.
    assert "removed untouched" in leave(app, owner="creator-session-id")
    assert not created.path.exists()


def test_enter_worktree_claims_it_for_the_new_session(repo, tmp_path, monkeypatch):
    from pcode.app import _enter_worktree

    monkeypatch.setattr(worktree, "run_setup", lambda *a, **k: None)
    path, identity = _enter_worktree(repo, "named")
    assert worktree.owners_of(worktree.describe(path)) == {identity}
    path, _ = _enter_worktree(repo, "forked", always=True, owner="existing")
    assert worktree.owners_of(worktree.describe(path)) == {"existing"}


def test_owner_record_lives_outside_the_checkout(repo):
    created = worktree.create(repo, "pcode-abc")
    assert worktree.owners_of(created) == set()
    worktree.claim(created, "someone")
    assert git(created.path, "status", "--porcelain") == ""
    assert worktree.is_untouched(created)


def test_a_session_started_by_new_adopts_the_worktree(repo, tmp_path):
    """`/new` before the creator saved a turn: the new session can still tidy it later."""
    created = worktree.create(repo, "pcode-abc")
    worktree.claim(created, "creator")
    worktree.adopt(created.path, "stranger", "intruder")  # Not the owner: ignored.
    worktree.adopt(created.path, "creator", "successor")
    assert worktree.owners_of(created) == {"creator", "successor"}
    session = SavedSession.create(
        "test:local", created.path, tmp_path / "sessions", identity="successor"
    )
    app = PreviewApp(workspace=created.path)
    app.runtime = FakeRuntime(session)
    assert "removed untouched" in leave(app)
    assert not created.path.exists()


def test_fork_worktree_is_claimed_for_the_copy(repo, tmp_path, monkeypatch):
    from pcode.host import fork_worktree

    monkeypatch.setattr(worktree, "run_setup", lambda *a, **k: None)
    saved = SavedSession.create("test:local", repo, tmp_path / "sessions")
    path = fork_worktree(saved, repo)
    assert worktree.owners_of(worktree.describe(path)) == {saved.info.id}
    saved.close()


def test_restore_recreates_a_session_worktree_on_its_surviving_branch(repo):
    created = worktree.create(repo, "pcode-abc")
    commit(created.path, "kept.txt")
    worktree.remove(created, force=True)
    note = worktree.restore(created.path, ("owner-id", "", "original-id"))
    assert note and "recreated it on branch pcode-abc" in note and "fresh" not in note
    assert (created.path / "kept.txt").exists()
    assert worktree.owners_of(worktree.describe(created.path)) == {"owner-id", "original-id"}
    assert worktree.restore(created.path) is None  # There already.


def test_restore_starts_a_fresh_branch_when_the_old_one_is_gone(repo):
    created = worktree.create(repo, "pcode-abc")
    worktree.remove(created, force=True)
    git(repo, "branch", "-D", "pcode-abc")
    note = worktree.restore(created.path)
    assert note and "started fresh from main" in note
    assert git(created.path, "rev-parse", "HEAD") == git(repo, "rev-parse", "main")


def test_restore_leaves_anything_but_a_session_worktree_alone(repo, tmp_path):
    hand = worktree.create(repo, "hand-made")
    worktree.remove(hand, force=True)
    assert worktree.restore(hand.path) is None
    assert worktree.restore(tmp_path / ".worktrees" / "pcode-x") is None  # No repository.
    assert not hand.path.exists()


def test_restore_waits_while_a_removal_holds_the_lock(repo):
    from pcode.task_worktrees import parent_operation

    created = worktree.create(repo, "pcode-abc")
    worktree.remove(created, force=True)
    with parent_operation(created.path, repo):
        assert worktree.restore(created.path) is None
    assert not created.path.exists()


def test_a_turn_recreates_a_workspace_deleted_from_elsewhere(repo, tmp_path):
    created, session, app = make(repo, tmp_path)
    worktree.remove(created, force=True)
    notes = []
    app.transcript.note = lambda text, **_: notes.append(text)
    app.controller.worktree_owner = "owner-id"
    asyncio.run(app.controller.restore_workspace())
    try:
        assert created.path.is_dir()
        assert any("recreated it on branch pcode-abc" in note for note in notes)
        owners = worktree.owners_of(worktree.describe(created.path))
        assert owners == {"owner-id", session.info.id}
    finally:
        session.close()


def test_a_turn_leaves_a_workspace_removed_on_purpose(repo, tmp_path):
    """`/worktree remove` repoints the session and flags it; neither is undone."""
    created, session, app = make(repo, tmp_path)
    worktree.remove(created, force=True)
    app.controller.workspace_removed = True
    asyncio.run(app.controller.restore_workspace())
    assert not created.path.exists()
    app.controller.workspace_removed = False
    session.info.workspace = str(repo)  # As `_leave_worktree` repoints it.
    asyncio.run(app.controller.restore_workspace())
    try:
        assert not created.path.exists()
    finally:
        session.close()


def test_owner_open_elsewhere_keeps_its_worktree(repo, tmp_path):
    """After `/stop`, the terminal can't open a session resumed elsewhere; it stays."""
    root = tmp_path / "sessions"
    created = worktree.create(repo, "pcode-abc")
    resumed = SavedSession.create("test:local", created.path, root)
    worktree.claim(created, resumed.info.id)
    app = PreviewApp(workspace=created.path, session_dir=root)
    app.runtime = FakeRuntime(None)
    out = leave(app, owner=resumed.info.id)
    assert "is still open in" in out and created.path.exists()
    resumed.close()
    assert "removed untouched" in leave(app, owner=resumed.info.id)
