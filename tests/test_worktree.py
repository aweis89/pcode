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


@pytest.mark.parametrize("location", ["main", "linked", "external", "subdir"])
def test_session_browser_groups_repository_worktrees(repo, tmp_path, location):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from pcode.session_ui import SessionBrowser
    from pcode.sessions import list_sessions

    linked = worktree.create(repo, "feature").path
    external = tmp_path / "external"
    git(repo, "worktree", "add", "-b", "external", str(external))
    subdir = linked / "nested"
    subdir.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    git(unrelated, "init", "-q")
    missing = tmp_path / "missing"
    paths = {"main": repo, "linked": linked, "external": external, "subdir": subdir}
    root = tmp_path / "sessions"
    ids = set()
    for path in [*paths.values(), unrelated, missing]:
        saved = SavedSession.create("test:local", path, root)
        saved.append("turn_started", prompt="Shared search term")
        saved.close()
        if path in paths.values():
            ids.add(saved.info.id)
    records = list_sessions(root)
    with (
        create_pipe_input() as pipe,
        patch("pcode.worktree.main_checkout", wraps=worktree.main_checkout) as resolve,
    ):
        browser = SessionBrowser(
            records, root=root, workspace=paths[location], input=pipe, output=DummyOutput()
        )
        assert browser.workspace == repo
        assert [info.id for info in browser.visible] == [r.id for r in records if r.id in ids]
        calls = resolve.call_count
        browser.query.text = "shared"
        assert {info.id for info in browser.visible} == ids
        browser.everywhere = True
        browser.refresh()
        assert browser.visible == records
        browser.everywhere = False
        browser.refresh()
        assert {info.id for info in browser.visible} == ids
        assert resolve.call_count == calls  # No git processes on search or redraw.


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
    assert "removed" in worktree.remove(created)
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
    assert identity and kwargs["workspace"] == repo / ".worktrees" / ("pcode-" + identity[:8])
    assert git(kwargs["workspace"], "symbolic-ref", "--short", "HEAD") == "pcode-" + identity[:8]


def test_cli_worktree_name_and_setting(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    cli(monkeypatch, "-m", "test:local", "-C", str(repo), "--worktree", "named")
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["workspace"] == repo / ".worktrees" / "pcode-named"

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
    assert not (repo / ".worktrees" / "pcode-broken").exists()


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


def test_worktree_merge_runs_under_a_system_row_with_a_terminal(repo):
    """With a live terminal the git work is deferred and labelled, not run inline."""
    import asyncio
    from types import SimpleNamespace

    created = worktree.create(repo, "feature")
    commit(created.path, "feature.txt")
    app = PreviewApp(workspace=created.path)
    notes, errors = [], []
    app.transcript.note = lambda text, **_: notes.append(text)
    app.transcript.error = lambda text, **_: errors.append(text)
    app.transcript.output = SimpleNamespace(app=SimpleNamespace(invalidate=lambda: None))

    app.worktree("merge")
    assert notes == [] and app.job_requested is not None
    label, detail, _ = app.job_requested
    assert (label, detail) == ("Merging worktree", "feature")

    seen = {}

    async def run():
        task = asyncio.ensure_future(app.perform_job())
        await asyncio.sleep(0)
        seen["kind"] = app.activity.prompt_kind
        seen["prompt"] = app.activity.prompt
        seen["state"] = app.activity.prompt_state
        seen["busy"] = app.activity.busy
        await task

    asyncio.run(run())
    assert seen == {
        "kind": "system",
        "prompt": "Merging worktree",
        "state": "running",
        "busy": True,
    }
    assert notes == ["merged feature into main"]
    assert app.activity.prompt_state == "done" and not app.activity.busy
    assert app.job_requested is None

    # A refusal surfaces as a command error and the row ends failed.
    commit(created.path, "README", "theirs\n")
    commit(repo, "README", "ours\n")
    app.worktree("merge")
    asyncio.run(app.perform_job())
    assert errors and "conflicts" in errors[-1]
    assert app.activity.prompt_state == "failed"


def test_worktree_merge_runs_mid_turn_without_taking_the_turns_row(repo):
    """A merge during a turn leaves the turn's live row and busy state alone."""
    import asyncio
    from types import SimpleNamespace

    created = worktree.create(repo, "feature")
    commit(created.path, "feature.txt")
    app = PreviewApp(workspace=created.path)
    notes, errors = [], []
    app.transcript.note = lambda text, **_: notes.append(text)
    app.transcript.error = lambda text, **_: errors.append(text)
    app.transcript.output = SimpleNamespace(app=SimpleNamespace(invalidate=lambda: None))
    app.activity.start_prompt("fix the bug")
    app.activity.busy = True

    app.worktree("merge")
    seen = {}

    async def run():
        task = asyncio.ensure_future(app.perform_job())
        await asyncio.sleep(0)
        seen["notice"] = app.activity.notice
        await task

    asyncio.run(run())
    assert seen["notice"] == "Merging worktree \u25b8 feature\u2026"
    assert notes == ["merged feature into main"] and not errors
    assert (repo / "feature.txt").exists()
    assert app.activity.prompt == "fix the bug"
    assert app.activity.prompt_kind == "user"
    assert app.activity.prompt_state == "running" and app.activity.busy
    assert app.activity.notice == ""

    # Actions that rewrite or delete the worktree still wait for the turn.
    with pytest.raises(ValueError, match="Wait for the current turn"):
        app.worktree("remove")


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


def test_conflicts_point_at_resolve_and_the_prompt_names_the_files(repo):
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from pcode.commands import SlashCompleter

    created = worktree.create(repo, "feature")
    assert worktree.conflicted_files(created.path) == []
    commit(created.path, "README", "theirs\n")
    commit(repo, "README", "ours\n")
    with pytest.raises(worktree.WorktreeError, match="conflicts .* README; /worktree resolve"):
        worktree.merge(created)
    assert worktree.conflicted_files(created.path) == ["README"]
    with pytest.raises(worktree.WorktreeError, match="already in progress"):
        worktree.merge(created)
    with pytest.raises(worktree.WorktreeError, match="already in progress"):
        worktree.finish(created)

    app = PreviewApp(workspace=created.path, model="test:local")
    app.worktree("resolve")
    prompt = app.skill_requested
    assert "`main` into `feature`" in prompt and "- README" in prompt
    assert "git merge --abort" in prompt
    offline = PreviewApp(workspace=created.path)
    with pytest.raises(ValueError, match="live model"):
        offline.worktree("resolve")

    git(created.path, "checkout", "--theirs", "README")
    git(created.path, "add", "README")
    git(created.path, "commit", "-q", "--no-edit")
    assert worktree.conflicted_files(created.path) == []
    with pytest.raises(ValueError, match="No merge conflicts"):
        app.worktree("resolve")
    assert "merged feature into main" in worktree.finish(created)

    completions = list(
        SlashCompleter(app.registry).get_completions(Document("/worktree re"), CompleteEvent())
    )
    assert {c.text: c.display_meta_text for c in completions} == {
        "resolve": "Ask the model to resolve the conflicts a merge stopped on",
        "remove": "Delete the merged worktree; the branch stays",
    }


def test_clean_removes_only_what_has_nothing_to_lose(repo):
    spent = worktree.create(repo, "spent")
    dirty = worktree.create(repo, "dirty")
    ahead = worktree.create(repo, "ahead")
    locked = worktree.create(repo, "locked")
    (dirty.path / "scratch").write_text("x")  # untracked is still work
    commit(ahead.path, "feature.txt")
    git(repo, "worktree", "lock", str(locked.path))

    report = "\n".join(worktree.clean(repo))
    assert f"removed {spent.path}" in report
    assert f"kept {dirty.path} (uncommitted or untracked files)" in report
    assert f"kept {ahead.path} (1 unmerged commit)" in report
    assert str(locked.path) not in report

    assert not spent.path.exists()
    assert git(repo, "branch", "--list", "spent") == ""
    assert dirty.path.exists() and ahead.path.exists() and locked.path.exists()
    assert "ahead" in git(repo, "branch", "--list", "ahead")
    assert sorted(worktree.clean(repo)) == sorted(
        [
            f"kept {dirty.path} (uncommitted or untracked files)",
            f"kept {ahead.path} (1 unmerged commit)",
        ]
    )


def test_clean_keeps_the_callers_own_worktree_and_prunes_stale_entries(repo):
    import shutil

    mine = worktree.create(repo, "mine")
    other = worktree.create(repo, "other")
    gone = worktree.create(repo, "gone")
    shutil.rmtree(gone.path)

    notes = []
    app = PreviewApp(workspace=mine.path)
    app.transcript.note = lambda text, **_: notes.append(text)
    app.worktree("clean")

    assert notes == [f"removed {other.path}"]
    assert mine.path.exists() and not other.path.exists()
    assert str(gone.path) not in worktree.listing(repo)
    # Called from inside a worktree, the caller's own directory is kept.
    assert worktree.clean(mine.path) == ["nothing to clean"]


def test_clean_reports_nothing_and_refuses_outside_a_repository(repo, tmp_path, capsys):
    assert worktree.clean(repo) == ["nothing to clean"]
    with pytest.raises(worktree.WorktreeError, match="not inside a git repository"):
        worktree.clean(tmp_path / "elsewhere")
    created = worktree.create(repo, "cli-clean")
    with patch("pathlib.Path.cwd", return_value=repo):
        assert worktree.main(["clean"]) == 0
    assert f"removed {created.path}" in capsys.readouterr().out


def test_run_setup_passes_environment(repo, monkeypatch):
    monkeypatch.setenv("KEEP_ME", "yes")
    user_script = preferences_path().parent / "worktree-setup"
    user_script.parent.mkdir(parents=True)
    user_script.write_text('#!/bin/sh\nprintf "%s %s" "$KEEP_ME" "$PCODE_MAIN" > out\n')
    os.chmod(user_script, 0o755)
    created = worktree.create(repo, "env")
    worktree.run_setup(created)
    assert (created.path / "out").read_text() == f"yes {repo}"
