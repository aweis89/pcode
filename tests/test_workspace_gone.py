"""A workspace deleted from outside stops the turn instead of spending retries."""

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from pcode.agent import create_agent
from pcode.app import main
from pcode.live import error_message
from pcode.sessions import SavedSession
from pcode.workspace import WorkspaceGoneError, require_workspace


def test_a_tool_call_in_a_deleted_workspace_ends_the_turn_once(tmp_path, monkeypatch):
    """The failure this replaces: three retries, then a message blaming the model."""
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    agent = create_agent("test", workspace)
    requests = 0

    async def respond(messages, info):
        nonlocal requests
        requests += 1
        yield {0: DeltaToolCall(name="shell", json_args='{"command": "ls"}')}

    shutil.rmtree(workspace)
    with pytest.raises(WorkspaceGoneError) as caught:
        agent.run_sync("look around", model=FunctionModel(stream_function=respond))
    # No correction was offered, so the model was never asked to try again.
    assert requests == 1
    message = error_message(caught.value)
    assert str(workspace) in message
    assert "--continue" in message
    # It is not the model's doing, and no budget would have helped.
    assert "retry limit" not in message
    assert "credentials" not in message


def test_require_workspace_passes_a_live_directory(tmp_path):
    require_workspace(tmp_path)
    with pytest.raises(WorkspaceGoneError, match="no longer exists"):
        require_workspace(tmp_path / "missing")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@x", "-c", "user.name=t", *args], check=True
    )


@pytest.fixture
def repo(tmp_path):
    path = (tmp_path / "repo").resolve()
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


def cli(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["pcode", *argv])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def test_continue_falls_back_to_the_project_checkout(repo, tmp_path, monkeypatch, capsys):
    from pcode import worktree

    created = worktree.create(repo, "pcode-gone")
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", created.path, root)
    identity = saved.info.id
    saved.close()
    worktree.remove(created, force=True)

    cli(monkeypatch, "--continue", identity, "--session-dir", str(root))
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["workspace"] == repo
    assert "no longer exists" in capsys.readouterr().err


def test_continue_without_any_surviving_directory_says_where_to_go(tmp_path, monkeypatch, capsys):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path / "gone", root)
    identity = saved.info.id
    saved.close()

    cli(monkeypatch, "--continue", identity, "--session-dir", str(root))
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert str(tmp_path / "gone") in error
    assert f"--continue {identity}" in error


def test_continue_elsewhere_in_the_same_repository_is_allowed(repo, tmp_path, monkeypatch):
    from pcode import worktree

    created = worktree.create(repo, "pcode-explicit")
    sibling = worktree.create(repo, "pcode-sibling")
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", created.path, root)
    identity = saved.info.id
    saved.close()
    worktree.remove(created, force=True)

    cli(monkeypatch, "--continue", identity, "--session-dir", str(root), "-C", str(sibling.path))
    with patch("pcode.app.PreviewApp") as app:
        main()
    # The deleted worktree cannot answer for its own repository; the recorded
    # project checkout does, so this is not mistaken for a cross-repo resume.
    assert app.call_args.kwargs["workspace"] == sibling.path


def test_continue_still_refuses_another_repository(repo, tmp_path, monkeypatch, capsys):
    from pcode import worktree

    created = worktree.create(repo, "pcode-foreign")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", created.path, root)
    identity = saved.info.id
    saved.close()
    worktree.remove(created, force=True)

    cli(monkeypatch, "--continue", identity, "--session-dir", str(root), "-C", str(elsewhere))
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    assert "cross-repo resume" in capsys.readouterr().err


def test_a_missing_workspace_flag_names_the_directory(tmp_path, monkeypatch, capsys):
    cli(monkeypatch, "-m", "test:local", "-C", str(tmp_path / "nowhere"))
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    assert str(tmp_path / "nowhere") in capsys.readouterr().err


def test_the_guard_reaches_a_delegated_worker(tmp_path, monkeypatch):
    """Sub-agents share the workspace, so they must stop on it too."""
    from pydantic_ai_harness.subagents import SubAgents

    from pcode.agent import create_coder
    from pcode.workspace import WorkspaceGuard

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    delegation = next(
        capability
        for capability in create_coder(tmp_path).capabilities
        if isinstance(capability, SubAgents)
    )
    worker = delegation.agents[0].agent
    assert any(
        isinstance(capability, WorkspaceGuard) for capability in worker.root_capability.capabilities
    )
