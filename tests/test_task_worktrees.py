"""Real Git lifecycle checks; no worker agents or user setup scripts are launched."""

import json
import multiprocessing
from pathlib import Path

import pytest
from test_worktree import commit, git
from test_worktree import repo as repo

from pcode import worktree
from pcode.task_worktrees import TaskWorktrees


def child(record):
    return Path(record.worktree)


def test_create_uses_parent_snapshot_and_persists(repo):
    parent = worktree.create(repo, "parent").path
    commit(parent, "parent-only")
    (parent / "secret").write_text("untracked data")
    manager = TaskWorktrees(parent)
    first, second = manager.create(), manager.create()
    assert first.base_commit == git(parent, "rev-parse", "HEAD")
    assert first.base_commit == second.base_commit
    assert child(first).parent == parent.parent
    assert (child(first) / "parent-only").exists()
    assert not (child(first) / "secret").exists()
    assert TaskWorktrees(parent).get(first.task_id) == first
    assert len(TaskWorktrees(parent).list()) == 2
    assert TaskWorktrees(repo).list() == []
    with pytest.raises(worktree.WorktreeError, match="belongs to parent"):
        TaskWorktrees(repo).get(first.task_id)


@pytest.mark.parametrize("staged", [False, True])
def test_create_refuses_tracked_changes(repo, staged):
    (repo / "README").write_text("changed")
    if staged:
        git(repo, "add", "README")
    with pytest.raises(worktree.WorktreeError, match="tracked changes"):
        TaskWorktrees(repo).create()


def test_create_refuses_detached_and_in_progress(repo):
    git(repo, "checkout", "--detach")
    with pytest.raises(worktree.WorktreeError, match="detached"):
        TaskWorktrees(repo).create()
    git(repo, "checkout", "main")
    (repo / ".git" / "rebase-merge").mkdir()
    with pytest.raises(worktree.WorktreeError, match="operation in progress"):
        TaskWorktrees(repo).create()


def test_record_exists_before_worktree_creation(repo, monkeypatch):
    manager = TaskWorktrees(repo)
    original = worktree.create

    def create(*args, **kwargs):
        records = list(manager.directory.glob("*.json"))
        assert len(records) == 1
        assert json.loads(records[0].read_text())["status"] == "running"
        return original(*args, **kwargs)

    monkeypatch.setattr(worktree, "create", create)
    manager.create()


def test_integration_targets_parent_not_mainline(repo):
    parent = worktree.create(repo, "parent").path
    commit(parent, "parent-only")
    main_head = git(repo, "rev-parse", "HEAD")
    manager = TaskWorktrees(parent)
    record = manager.create()
    commit(child(record), "worker-result")
    finished = manager.finish(record.task_id, "completed", "Done")
    assert finished.summary == "Done"
    assert not finished.dirty
    assert finished.head_commit == git(child(record), "rev-parse", "HEAD")
    assert manager.integrate(record.task_id).status == "integrated"
    assert (parent / "worker-result").exists()
    assert git(repo, "rev-parse", "HEAD") == main_head
    assert child(record).exists()
    assert manager.integrate(record.task_id).status == "integrated"
    assert manager.discard(record.task_id).status == "discarded"
    assert not child(record).exists()


def test_noop_integration(repo):
    manager = TaskWorktrees(repo)
    record = manager.create()
    manager.finish(record.task_id, "completed")
    assert manager.integrate(record.task_id).status == "integrated"


def test_conflict_preserves_child_and_manual_resolution_is_recognized(repo):
    manager = TaskWorktrees(repo)
    record = manager.create()
    commit(child(record), "README", "worker\n")
    manager.finish(record.task_id, "completed")
    commit(repo, "README", "parent\n")
    with pytest.raises(worktree.WorktreeError, match="Resolve and commit there"):
        manager.integrate(record.task_id)
    assert manager.get(record.task_id).status == "conflicted"
    assert child(record).exists()
    with pytest.raises(worktree.WorktreeError, match="operation in progress"):
        manager.integrate(record.task_id)
    (repo / "README").write_text("both\n")
    git(repo, "add", "README")
    git(repo, "commit", "--no-edit")
    assert manager.integrate(record.task_id).status == "integrated"


@pytest.mark.parametrize("target", ["parent", "child"])
def test_integrate_refuses_untracked_changes(repo, target):
    manager = TaskWorktrees(repo)
    record = manager.create()
    manager.finish(record.task_id, "completed")
    path = repo if target == "parent" else child(record)
    (path / "untracked").write_text("keep")
    with pytest.raises(worktree.WorktreeError, match="dirty"):
        manager.integrate(record.task_id)


def test_changed_parent_branch_child_branch_and_head_refused(repo):
    manager = TaskWorktrees(repo)
    record = manager.create()
    manager.finish(record.task_id, "completed")
    git(repo, "checkout", "-b", "other-parent")
    with pytest.raises(worktree.WorktreeError, match="Parent changed branch"):
        manager.integrate(record.task_id)
    git(repo, "checkout", "main")
    git(child(record), "checkout", "-b", "other-child")
    with pytest.raises(worktree.WorktreeError, match="Worker changed branch"):
        manager.integrate(record.task_id)
    with pytest.raises(worktree.WorktreeError, match="Worker changed branch"):
        manager.discard(record.task_id, confirm=True)
    git(child(record), "checkout", record.branch)
    commit(child(record), "late")
    with pytest.raises(worktree.WorktreeError, match="HEAD changed"):
        manager.integrate(record.task_id)


def test_finish_observes_dirty_head_and_changed_branch(repo):
    manager = TaskWorktrees(repo)
    record = manager.create()
    commit(child(record), "result")
    git(child(record), "checkout", "-b", "unexpected")
    (child(record) / "dirty").write_text("keep")
    result = manager.finish(record.task_id, "completed", "Worker says done")
    assert result.status == "failed"
    assert result.dirty
    assert result.head_commit == git(child(record), "rev-parse", "HEAD")
    assert "Worker says done" in result.summary
    assert "changed branch" in result.summary


def test_discard_requires_confirmation_and_never_touches_parent_changes(repo):
    manager = TaskWorktrees(repo)
    record = manager.create()
    with pytest.raises(worktree.WorktreeError, match="active"):
        manager.discard(record.task_id, confirm=True)
    (child(record) / "dirty").write_text("partial")
    manager.finish(record.task_id, "failed")
    (repo / "README").write_text("parent edits")
    with pytest.raises(worktree.WorktreeError, match="confirm=True"):
        manager.discard(record.task_id)
    assert manager.discard(record.task_id, confirm=True).status == "discarded"
    assert (repo / "README").read_text() == "parent edits"


def test_stale_running_record_recovers_as_failed(repo):
    manager = TaskWorktrees(repo)
    record = manager.create()
    (child(record) / "partial").write_text("keep")
    record.process_started = -1  # PID reuse must not make an interrupted worker active
    manager._save(record)
    recovered = TaskWorktrees(repo).list()[0]
    assert recovered.status == "failed"
    assert recovered.dirty
    assert "interrupted" in recovered.summary
    assert child(record).exists()
    assert TaskWorktrees(repo).get(record.task_id) == recovered


def test_invalid_ids_and_record_paths_are_rejected(repo):
    manager = TaskWorktrees(repo)
    with pytest.raises(worktree.WorktreeError, match="Invalid task id"):
        manager.get("../../HEAD")
    record = manager.create()
    record.worktree = str(repo)
    manager._save(record)
    with pytest.raises(worktree.WorktreeError, match="invalid record paths"):
        manager.discard(record.task_id, confirm=True)
    assert (repo / "README").exists()


def test_creation_failure_remains_recoverable(repo, monkeypatch):
    manager = TaskWorktrees(repo)

    def fail(*args, **kwargs):
        raise worktree.WorktreeError("setup could not create checkout")

    monkeypatch.setattr(worktree, "create", fail)
    with pytest.raises(worktree.WorktreeError, match="could not create"):
        manager.create()
    record = manager.list()[0]
    assert record.status == "failed"
    assert manager.discard(record.task_id, confirm=True).status == "discarded"


def test_rewritten_child_history_is_not_integrated(repo):
    manager = TaskWorktrees(repo)
    record = manager.create()
    git(child(record), "checkout", "--orphan", "unrelated")
    git(child(record), "commit", "-m", "unrelated root")
    new_head = git(child(record), "rev-parse", "HEAD")
    git(child(record), "checkout", record.branch)
    git(child(record), "reset", "--hard", new_head)
    manager.finish(record.task_id, "completed")
    with pytest.raises(worktree.WorktreeError, match="recorded base"):
        manager.integrate(record.task_id)


def test_discard_does_not_delete_branch_checked_out_elsewhere(repo, tmp_path):
    manager = TaskWorktrees(repo)
    record = manager.create()
    manager.finish(record.task_id, "failed")
    moved = tmp_path / "moved"
    git(repo, "worktree", "move", record.worktree, str(moved))
    with pytest.raises(worktree.WorktreeError, match="git branch failed"):
        manager.discard(record.task_id, confirm=True)
    assert moved.exists()
    assert git(moved, "branch", "--show-current") == record.branch
    assert manager.get(record.task_id).status != "discarded"


def test_finish_preserves_dirty_observation_when_head_fails(repo, monkeypatch):
    from pcode import task_worktrees

    manager = TaskWorktrees(repo)
    record = manager.create()
    (child(record) / "partial").write_text("work")

    def fail_head(path):
        raise worktree.WorktreeError("HEAD unavailable")

    monkeypatch.setattr(task_worktrees, "_head", fail_head)
    result = manager.finish(record.task_id, "failed", "Worker failed")
    assert result.dirty
    assert "HEAD unavailable" in result.summary
    assert "Worker failed" in result.summary


def _hold_parent_lock(parent, ready, release):
    with TaskWorktrees(Path(parent))._lock():
        ready.set()
        release.wait(10)


def test_cross_process_lock_coordinates_snapshot_and_integration(repo):
    manager = TaskWorktrees(repo)
    record = manager.create()
    manager.finish(record.task_id, "completed")
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold_parent_lock, args=(str(repo), ready, release))
    process.start()
    try:
        assert ready.wait(10)
        for operation in (manager.create, lambda: manager.integrate(record.task_id)):
            with pytest.raises(worktree.WorktreeError, match="Another task operation"):
                operation()
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0
    assert manager.integrate(record.task_id).status == "integrated"


@pytest.mark.parametrize("recreate", [False, True])
def test_create_revalidates_removed_checkout_under_lock(repo, recreate):
    parent = worktree.create(repo, "parent")
    manager = TaskWorktrees(parent.path)
    worktree.remove(parent)
    if recreate:
        parent.path.mkdir()
    with pytest.raises(worktree.WorktreeError, match="no longer"):
        manager.create()
    assert manager.list() == []


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_finish_wait_persists_terminal_state_after_contention(repo, monkeypatch, status):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from pcode import task_worktrees

    manager = TaskWorktrees(repo)
    record = manager.create()
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold_parent_lock, args=(str(repo), ready, release))
    process.start()
    entered = Event()
    flock = task_worktrees.fcntl.flock

    def observe_wait(fd, operation):
        if operation == task_worktrees.fcntl.LOCK_EX:
            entered.set()
        return flock(fd, operation)

    monkeypatch.setattr(task_worktrees.fcntl, "flock", observe_wait)
    try:
        assert ready.wait(10)
        with pytest.raises(worktree.WorktreeError, match="Another task operation"):
            manager.finish(record.task_id, status)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(manager.finish, record.task_id, status, "done", wait=True)
            try:
                assert entered.wait(10)
                assert not pending.done()
                assert manager._read(record.task_id).status == "running"
            finally:
                release.set()
            assert pending.result(timeout=10).status == status
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0
    assert manager.get(record.task_id).status == status
    assert manager.get(record.task_id).summary == "done"


def _finish_from_other_process(parent, task_id, result):
    try:
        TaskWorktrees(Path(parent)).finish(task_id, "completed", wait=True)
    except worktree.WorktreeError as error:
        result.put(str(error))
    else:
        result.put("finished")


def test_other_process_cannot_finalize_active_worker(repo):
    manager = TaskWorktrees(repo)
    record = manager.create()
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    process = context.Process(
        target=_finish_from_other_process, args=(str(repo), record.task_id, result)
    )
    process.start()
    try:
        assert "another active process" in result.get(timeout=10)
    finally:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0
    assert manager.get(record.task_id).status == "running"
