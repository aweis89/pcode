import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from pcode import worktree
from pcode.app import PreviewApp, main
from pcode.preferences import preferences_path, save_preferences
from pcode.sessions import SavedSession

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is required",
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # Git resolves symlinks (tmp on macOS lives under /private), so compare
    # against the resolved path everywhere.
    path = (tmp_path / "repo").resolve()
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "README").write_text("hello\n")
    git(path, "add", "README")
    git(path, "commit", "-q", "-m", "init")
    monkeypatch.delenv("GIT_DIR", raising=False)
    return path


def commit(path: Path, name: str, text: str = "x\n") -> None:
    (path / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-q", "-m", f"add {name}")


def test_create_describes_and_lists(repo):
    created = worktree.create(repo, "feature")
    assert created.path == repo / ".worktrees" / "feature"
    assert created.branch == "feature"
    assert created.main == repo
    assert git(created.path, "symbolic-ref", "--short", "HEAD") == "feature"
    assert worktree.describe(repo) is None
    assert worktree.describe(created.path) == created
    assert worktree.is_linked(created.path)
    assert not worktree.is_linked(repo)
    assert "feature" in worktree.listing(repo)


def test_create_reuses_existing_branch_and_rejects_bad_names(repo):
    git(repo, "branch", "old")
    assert worktree.create(repo, "old").branch == "old"
    with pytest.raises(worktree.WorktreeError, match="already exists"):
        worktree.create(repo, "old")
    for name in ("a/b", ".hidden", "", "has space"):
        with pytest.raises(worktree.WorktreeError, match="plain name"):
            worktree.create(repo, name)


def test_outside_git_is_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert worktree.main_checkout(plain) is None
    assert worktree.describe(plain) is None
    with pytest.raises(worktree.WorktreeError, match="not inside a git repository"):
        worktree.create(plain, "x")


def test_merge_fast_forwards_mainline(repo):
    created = worktree.create(repo, "feature")
    commit(created.path, "feature.txt")
    commit(repo, "main.txt")  # mainline moved: merge must bring it in first
    assert worktree.unmerged_commits(created) == 1
    assert worktree.merge(created) == "merged feature into main"
    assert (repo / "feature.txt").exists()
    assert (created.path / "main.txt").exists()
    assert worktree.unmerged_commits(created) == 0


def test_merge_refuses_dirty_worktree_and_conflicts(repo):
    created = worktree.create(repo, "feature")
    (created.path / "README").write_text("dirty\n")
    with pytest.raises(worktree.WorktreeError, match="uncommitted"):
        worktree.merge(created)
    git(created.path, "checkout", "README")
    commit(created.path, "README", "theirs\n")
    commit(repo, "README", "ours\n")
    with pytest.raises(worktree.WorktreeError, match="conflicts"):
        worktree.merge(created)
    # The mainline checkout is untouched; the conflict lives in the worktree.
    assert (repo / "README").read_text() == "ours\n"
    assert git(repo, "status", "--porcelain") == ""


def test_merge_refuses_when_mainline_has_conflicting_edits(repo):
    created = worktree.create(repo, "feature")
    commit(created.path, "README", "theirs\n")
    (repo / "README").write_text("uncommitted\n")
    with pytest.raises(worktree.WorktreeError, match="fast-forward"):
        worktree.merge(created)


def test_remove_keeps_branch_and_never_forces_user_work(repo):
    created = worktree.create(repo, "feature")
    (created.path / "scratch").write_text("x")
    with pytest.raises(worktree.WorktreeError, match="--force"):
        worktree.remove(created)
    assert created.path.exists()
    (created.path / "scratch").unlink()
    assert "kept" in worktree.remove(created)
    assert not created.path.exists()
    assert "feature" in git(repo, "branch", "--list", "feature")


def test_setup_scripts_user_then_project_gated(repo):
    user_script = preferences_path().parent / "worktree-setup"
    user_script.parent.mkdir(parents=True)
    user_script.write_text('#!/bin/sh\necho user >> "$PCODE_WORKTREE/log"\n')
    project = repo / ".pcode"
    project.mkdir()
    # Not executable: run through sh.
    (project / "worktree-setup").write_text(
        'echo "project $PCODE_BRANCH $(pwd)" >> log; test -d "$PCODE_MAIN"\n'
    )
    created = worktree.create(repo, "feature")
    assert worktree.setup_scripts(created) == [user_script]
    save_preferences(project_extensions="on")
    assert worktree.setup_scripts(created) == [user_script, project / "worktree-setup"]
    worktree.run_setup(created)
    assert (created.path / "log").read_text().splitlines() == [
        "user",
        f"project feature {created.path}",
    ]
    # A committed script is read from the checkout itself, so a branch that
    # changes it is set up by its own version.
    committed = repo / ".pcode" / "worktree-setup"
    committed.write_text("echo committed >> log\n")
    git(repo, "add", ".pcode")
    git(repo, "commit", "-q", "-m", "setup")
    committed.write_text("echo local >> log\n")
    other = worktree.create(repo, "other")
    assert worktree.setup_scripts(other) == [user_script, other.path / ".pcode" / "worktree-setup"]
    worktree.run_setup(other)
    assert (other.path / "log").read_text().splitlines() == ["user", "committed"]


def test_setup_script_failure_raises(repo):
    save_preferences(project_extensions="on")
    (repo / ".pcode").mkdir()
    (repo / ".pcode" / "worktree-setup").write_text("exit 3\n")
    created = worktree.create(repo, "feature")
    with pytest.raises(worktree.WorktreeError, match="exited 3"):
        worktree.run_setup(created)


def cli(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["pcode", *argv])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def test_cli_worktree_flag_names_after_session(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    cli(monkeypatch, "-m", "test:local", "-C", str(repo), "--worktree")
    with patch("pcode.app.PreviewApp") as app:
        main()
    kwargs = app.call_args.kwargs
    identity = kwargs["session_id"]
    assert identity and kwargs["workspace"] == repo / ".worktrees" / identity[:8]
    assert git(kwargs["workspace"], "symbolic-ref", "--short", "HEAD") == identity[:8]


def test_cli_worktree_name_and_setting(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    cli(monkeypatch, "-m", "test:local", "-C", str(repo), "--worktree", "named")
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["workspace"] == repo / ".worktrees" / "named"

    save_preferences(worktree="on")
    cli(monkeypatch, "-m", "test:local", "-C", str(repo))
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["workspace"].parent == repo / ".worktrees"

    cli(monkeypatch, "-m", "test:local", "-C", str(repo), "--no-worktree")
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["workspace"] == repo
    assert app.call_args.kwargs["session_id"] is None


def test_cli_worktree_setting_skips_linked_and_non_git(repo, monkeypatch, tmp_path):
    save_preferences(worktree="on")
    inside = worktree.create(repo, "already").path
    cli(monkeypatch, "-m", "test:local", "-C", str(inside))
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["workspace"] == inside
    plain = tmp_path / "plain"
    plain.mkdir()
    cli(monkeypatch, "-m", "test:local", "-C", str(plain))
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["workspace"] == plain


def test_cli_worktree_flag_outside_git_fails(tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    cli(monkeypatch, "-m", "test:local", "-C", str(plain), "--worktree")
    with pytest.raises(SystemExit):
        main()
    assert "needs a git repository" in capsys.readouterr().err


def test_cli_failed_setup_removes_worktree(repo, monkeypatch, capsys):
    save_preferences(project_extensions="on")
    (repo / ".pcode").mkdir()
    (repo / ".pcode" / "worktree-setup").write_text("touch junk; exit 1\n")
    cli(monkeypatch, "-m", "test:local", "-C", str(repo), "--worktree", "broken")
    with pytest.raises(SystemExit):
        main()
    assert "exited 1" in capsys.readouterr().err
    assert not (repo / ".worktrees" / "broken").exists()


def test_cli_resume_never_creates_worktree(repo, monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", repo, root)
    identity = saved.info.id
    saved.close()
    save_preferences(worktree="on")
    cli(monkeypatch, "-c", identity, "--session-dir", str(root))
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["workspace"] == repo
    assert not (repo / ".worktrees").exists()


def test_first_session_takes_worktree_identity(tmp_path):
    root = tmp_path / "sessions"
    app = PreviewApp(model="test:local", workspace=tmp_path, save=True, session_dir=root)
    app._session_id = "11111111-2222-3333-4444-555555555555"
    first = app._create_session()
    second = app._create_session()
    try:
        assert first.info.id == "11111111-2222-3333-4444-555555555555"
        assert second.info.id != first.info.id
    finally:
        first.close()
        second.close()


def test_worktree_command(repo):
    created = worktree.create(repo, "feature")
    app = PreviewApp(workspace=created.path)
    notes = []
    app.transcript.note = lambda text, **_: notes.append(text)
    app.worktree("")
    assert notes[0] == f"Worktree: {created.path} (branch feature)"
    assert notes[2] == "0 unmerged commits"
    commit(created.path, "feature.txt")
    with pytest.raises(ValueError, match="unmerged"):
        app.worktree("remove")
    app.worktree("merge")
    assert notes[-1] == "merged feature into main"
    app.worktree("remove")
    assert not created.path.exists()
    assert "no longer exists" in notes[-1]

    mainline = PreviewApp(workspace=repo)
    mainline.transcript.note = lambda text, **_: notes.append(text)
    mainline.worktree("")
    assert "not a linked worktree" in notes[-1]
    with pytest.raises(ValueError, match="Usage"):
        mainline.registry.dispatch("/worktree bogus")


def test_module_cli_runs_project_setup_unconditionally(repo, monkeypatch, capsys):
    (repo / ".pcode").mkdir()
    (repo / ".pcode" / "worktree-setup").write_text("touch from-setup\n")
    monkeypatch.chdir(repo)
    assert worktree.main(["new", "cli"]) == 0
    assert (repo / ".worktrees" / "cli" / "from-setup").exists()
    assert "worktree ready" in capsys.readouterr().out
    commit(repo / ".worktrees" / "cli", "f.txt")
    assert worktree.main(["merge", "cli"]) == 0
    assert (repo / "f.txt").exists()
    assert worktree.main(["remove", "cli"]) == 1  # untracked setup output blocks it
    (repo / ".worktrees" / "cli" / "from-setup").unlink()
    assert worktree.main(["remove", "cli"]) == 0
    assert not (repo / ".worktrees" / "cli").exists()
    assert worktree.main(["remove", "cli"]) == 1
    assert "no worktree named cli" in capsys.readouterr().err
    assert worktree.main(["list"]) == 0


def test_run_setup_passes_environment(repo, monkeypatch):
    monkeypatch.setenv("KEEP_ME", "yes")
    user_script = preferences_path().parent / "worktree-setup"
    user_script.parent.mkdir(parents=True)
    user_script.write_text('#!/bin/sh\nprintf "%s %s" "$KEEP_ME" "$PCODE_MAIN" > out\n')
    os.chmod(user_script, 0o755)
    created = worktree.create(repo, "env")
    worktree.run_setup(created)
    assert (created.path / "out").read_text() == f"yes {repo}"
