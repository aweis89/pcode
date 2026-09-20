"""The post-merge hook that pushes the mainline branch after a merge."""

import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "post-merge"

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
    monkeypatch.delenv("GIT_DIR", raising=False)
    remote = (tmp_path / "remote.git").resolve()
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))

    path = (tmp_path / "repo").resolve()
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    hooks = path / ".githooks"
    hooks.mkdir()
    shutil.copy(HOOK, hooks / "post-merge")
    git(path, "config", "core.hooksPath", ".githooks")
    (path / "README").write_text("hello\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    git(path, "remote", "add", "origin", str(remote))
    git(path, "push", "-q", "-u", "origin", "main")
    return path


def commit_on_branch(path: Path, branch: str, text: str) -> None:
    git(path, "checkout", "-q", "-b", branch)
    (path / "file").write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", text)
    git(path, "checkout", "-q", "main")


def remote_head(path: Path) -> str:
    return git(path, "rev-parse", "origin/main")


def test_merge_on_mainline_pushes(repo):
    commit_on_branch(repo, "feature", "work")
    git(repo, "merge", "--no-edit", "-q", "feature")
    assert remote_head(repo) == git(repo, "rev-parse", "HEAD")


def test_fast_forward_merge_pushes(repo):
    commit_on_branch(repo, "feature", "work")
    git(repo, "merge", "--ff-only", "-q", "feature")
    assert remote_head(repo) == git(repo, "rev-parse", "HEAD")


def test_merge_inside_a_linked_worktree_does_not_push(repo, tmp_path):
    linked = (tmp_path / "linked").resolve()
    git(repo, "worktree", "add", "-q", "-b", "feature", str(linked))
    (linked / "file").write_text("work")
    git(linked, "add", "-A")
    git(linked, "commit", "-q", "-m", "work")
    before = remote_head(repo)

    # Merging the mainline into the branch must not publish anything.
    git(linked, "merge", "--no-edit", "-q", "main")
    assert remote_head(repo) == before
    assert "refs/heads/feature" not in git(repo, "ls-remote", "origin")


def test_opt_out_env_var_skips_the_push(repo, monkeypatch):
    commit_on_branch(repo, "feature", "work")
    before = remote_head(repo)
    monkeypatch.setenv("PCODE_NO_AUTOPUSH", "1")
    git(repo, "merge", "--no-edit", "-q", "feature")
    assert remote_head(repo) == before


def test_pull_without_local_commits_is_a_no_op(repo):
    # Nothing is ahead of the upstream, so the hook must not attempt a push.
    result = subprocess.run(
        ["git", "-C", str(repo), "pull", "-q", "--no-rebase", "origin", "main"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "post-merge" not in result.stderr
