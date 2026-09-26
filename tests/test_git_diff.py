import os
import subprocess
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from pcode import git_diff, worktree
from pcode.app import PreviewApp
from pcode.git_diff import GitDiffError, session_diff
from pcode.runtime import EditCompleted

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is required",
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(path: Path, name: str, text: str, message: str = "change") -> None:
    (path / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # Git resolves symlinks (tmp on macOS lives under /private).
    path = (tmp_path / "repo").resolve()
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    commit(path, "README", "hello\n", "init")
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    return path


@pytest.fixture
def linked(repo):
    return worktree.create(repo, "feature")


def by_path(view):
    return {change.path: change for change in view.changes}


def untouched(path: Path):
    """Porcelain status and the git dir listing, to prove the view wrote neither."""
    git_dir = Path(git(path, "rev-parse", "--path-format=absolute", "--git-dir"))
    return git(path, "status", "--porcelain=v1", "-uall"), sorted(os.listdir(git_dir))


def test_branch_diff_is_net_per_file_and_ignores_merged_mainline(repo, linked):
    commit(linked.path, "README", "hello\nfirst\n")
    (linked.path / "README").write_text("hello\nfirst\nsecond\n")  # uncommitted
    (linked.path / "new.py").write_text("print('new')\n")  # untracked
    commit(repo, "main_only.py", "from main\n")
    git(linked.path, "merge", "-q", "--no-edit", "main")
    before = untouched(linked.path)

    view = session_diff(linked.path, [])

    assert untouched(linked.path) == before
    changes = by_path(view)
    assert list(changes) == ["README", "new.py"]  # mainline's file is not the branch's work
    readme = changes["README"]
    assert (readme.operation, readme.added, readme.removed) == ("edited", 2, 0)
    assert "+first" in readme.patch and "+second" in readme.patch
    assert "diff --git" not in readme.patch and "\nindex " not in readme.patch
    assert changes["new.py"].operation == "created"
    # Merging mainline in moved the merge-base up to mainline's tip.
    base = git(linked.path, "rev-parse", "--short", "main")
    assert view.title == (
        f"Git diff · feature vs main (merge-base {base}) · includes uncommitted and new files"
    )


def test_merged_branch_has_nothing_to_show(repo, linked):
    commit(linked.path, "done.py", "x\n")
    worktree.merge(linked)
    view = session_diff(linked.path, [])
    assert view.changes == []
    assert view.empty == "feature has nothing that main does not already have."


def test_renames_deletions_type_and_mode_changes_line_up_with_their_patches(repo):
    path = repo  # committed on mainline, so the merge-base has them
    for name, text in [
        ("old.py", "one\ntwo\nthree\nfour\n"),
        ("gone.py", "bye\n"),
        ("kind.txt", "file\n"),
        ("mode.sh", "echo\n"),
        ("sp ace.py", "a\n"),
    ]:
        (path / name).write_text(text)
    (path / "bin.dat").write_bytes(b"\x00\x01")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "files")
    path = worktree.create(repo, "feature").path
    git(path, "mv", "old.py", "new.py")
    (path / "new.py").write_text("one\ntwo\nthree\nfour\nfive\n")
    (path / "gone.py").unlink()
    (path / "kind.txt").unlink()
    (path / "kind.txt").symlink_to("README")  # a type change prints two patches
    (path / "mode.sh").chmod(0o755)
    (path / "bin.dat").write_bytes(b"\x00\x02")
    (path / "sp ace.py").write_text("a\nb\n")
    (path / "ünï.py").write_text("u\n")

    changes = by_path(session_diff(path, []))

    assert changes["old.py → new.py"].operation == "renamed"
    assert "+five" in changes["old.py → new.py"].patch
    assert changes["gone.py"].operation == "deleted" and "-bye" in changes["gone.py"].patch
    assert "+README" in changes["kind.txt"].patch and "-file" in changes["kind.txt"].patch
    assert "new mode 100755" in changes["mode.sh"].patch
    assert "Binary files" in changes["bin.dat"].patch
    assert "+b" in changes["sp ace.py"].patch
    assert changes["ünï.py"].operation == "created" and "+u" in changes["ünï.py"].patch


def test_secrets_stay_out_of_the_view_and_the_object_store(repo, linked):
    path = linked.path
    commit(path, "config.env", "A=1\n")
    (path / "config.env").write_text("A=2\n")
    (path / ".env.local").write_text("TOKEN=synthetic-untracked\n")
    (path / "app.py").write_text('token = "synthetic-secret"\n')

    changes = by_path(session_diff(path, []))

    unread = changes[".env.local"]
    assert (unread.operation, unread.omitted, unread.patch) == (
        "created",
        "Sensitive file; not read",
        "",
    )
    tracked = changes["config.env"]
    assert tracked.omitted == "Sensitive file" and tracked.patch == ""
    assert (tracked.operation, tracked.added, tracked.removed) == ("created", 1, 0)
    assert "synthetic-secret" not in changes["app.py"].patch
    assert "[redacted]" in changes["app.py"].patch
    blob = git(path, "hash-object", ".env.local")  # computes the id without writing it
    missing = subprocess.run(["git", "-C", str(path), "cat-file", "-e", blob], capture_output=True)
    assert missing.returncode != 0


def test_large_diffs_are_clipped_or_only_counted(repo, linked, monkeypatch):
    (linked.path / "long.py").write_text("".join(f"line {i}\n" for i in range(2500)))
    change = by_path(session_diff(linked.path, []))["long.py"]
    assert change.truncated and change.added == 2500
    assert len(change.patch.splitlines()) == git_diff.MAX_DIFF_LINES

    monkeypatch.setattr(git_diff, "MAX_FILE_DIFF", 100)
    change = by_path(session_diff(linked.path, []))["long.py"]
    assert change.omitted == "Diff exceeds preview size limit" and change.added == 2500


def test_shared_checkout_shows_only_edited_files_against_head(repo):
    (repo / "sub").mkdir()
    commit(repo, "sub/edited.py", "old\n")
    commit(repo, "users.py", "theirs\n")
    git(repo, "commit", "-q", "--allow-empty", "-m", "head")
    (repo / "users.py").write_text("theirs, still dirty\n")  # not this session's
    (repo / "sub" / "edited.py").write_text("new\n")
    (repo / "sub" / "created.py").write_text("made\n")
    (repo / "sub" / "untouched.py").write_text("stray\n")
    workspace = repo / "sub"
    before = untouched(repo)

    view = session_diff(workspace, ["edited.py", "created.py", str(repo / "sub" / "edited.py")])

    assert untouched(repo) == before
    assert list(by_path(view)) == ["sub/created.py", "sub/edited.py"]
    short = git(repo, "rev-parse", "--short", "HEAD")
    assert view.title == (
        f"Git diff · uncommitted changes vs HEAD ({short}) · only files edited this session"
    )
    assert session_diff(workspace, []).changes == []
    assert session_diff(workspace, ["../../elsewhere.py"]).changes == []


def test_outside_git_there_is_no_view_and_a_repository_without_commits_errors(tmp_path):
    assert session_diff(tmp_path, ["a.py"]) is None
    git(tmp_path, "init", "-q")
    with pytest.raises(GitDiffError, match="no commits"):
        session_diff(tmp_path, ["a.py"])


def edited(path: str, call_id: str) -> EditCompleted:
    return EditCompleted(call_id, path, "edited", "@@ -1 +1 @@\n-a\n+b", 1, 1)


def test_app_prefers_the_git_view_and_falls_back_to_tool_edits(repo, linked, tmp_path, monkeypatch):
    (linked.path / "new.py").write_text("x\n")
    app = PreviewApp(console=Console(file=StringIO()), workspace=linked.path)
    assert [change.path for change in app.diff_view().changes] == ["new.py"]

    outside = tmp_path / "plain"
    outside.mkdir()
    app = PreviewApp(console=Console(file=StringIO()), workspace=outside)
    app.present_events((edited("first.py", "1"), edited("second.py", "2")))
    view = app.diff_view()
    assert view.title == "Tool edits, newest first"
    assert [change.path for change in view.changes] == ["second.py", "first.py"]

    def broken(*args):
        raise GitDiffError("git diff failed: boom")

    monkeypatch.setattr(git_diff, "session_diff", broken)
    view = app.diff_view()
    assert view.title == "Tool edits, newest first · git diff unavailable: git diff failed: boom"
    assert len(view.changes) == 2
