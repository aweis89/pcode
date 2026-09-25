"""Persistent isolated workers; results are integrated into their owning checkout only.

Records live in the shared Git directory. A parent lock serializes snapshots,
record transitions and merges across processes (and refuses concurrent callers).
Setup and execution deliberately belong to the caller, after create returns.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil

from pcode import worktree as wt

_STATUSES = {"running", "completed", "failed", "cancelled", "conflicted", "integrated", "discarded"}


@dataclass
class TaskRecord:
    task_id: str
    parent: str
    parent_branch: str
    base_commit: str
    head_commit: str | None
    branch: str
    worktree: str
    status: str
    dirty: bool
    summary: str = ""
    pid: int = 0
    process_started: float = 0


def _branch(path: Path) -> str:
    result = wt._git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if result.returncode:
        raise wt.WorktreeError(f"{path} has detached HEAD; check out the expected branch first")
    return result.stdout.strip()


def _head(path: Path) -> str:
    return wt._git(path, "rev-parse", "HEAD").stdout.strip()


def _dirty(path: Path, *, tracked: bool = False) -> bool:
    args = ["status", "--porcelain", "--untracked-files=no" if tracked else "--untracked-files=all"]
    return bool(wt._git(path, *args).stdout.strip())


def _idle(path: Path) -> None:
    for name in (
        "MERGE_HEAD",
        "rebase-merge",
        "rebase-apply",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "sequencer",
    ):
        location = wt._git(path, "rev-parse", "--path-format=absolute", "--git-path", name)
        if Path(location.stdout.strip()).exists():
            raise wt.WorktreeError(f"Git operation in progress in {path}; finish or abort it first")


def _ancestor(repo: Path, older: str, newer: str) -> bool:
    return wt._git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def _alive(record: TaskRecord) -> bool:
    try:
        process = psutil.Process(record.pid)
        return process.create_time() == record.process_started and process.is_running()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


class TaskWorktrees:
    def __init__(self, parent: Path):
        self.parent = parent.resolve()
        self.main = wt.main_checkout(self.parent)
        if self.main is None:
            raise wt.WorktreeError(f"{self.parent} is not a Git checkout")
        # Metadata and locks also serve bare repositories and removed parents.
        # Only create() requires a live checkout root.
        common = wt._git(self.parent, "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.directory = Path(common.stdout.strip()) / "pcode-tasks"
        self.directory.mkdir(exist_ok=True)

    @contextmanager
    def _lock(self, *, wait: bool = False):
        key = hashlib.sha256(str(self.parent).encode()).hexdigest()
        with (self.directory / f"parent-{key}.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError as error:
                raise wt.WorktreeError(
                    f"Another task operation is using parent {self.parent}; retry when it finishes"
                ) from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _path(self, task_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise wt.WorktreeError("Invalid task id; use an id from the task list")
        return self.directory / f"{task_id}.json"

    def _read(self, task_id: str) -> TaskRecord:
        path = self._path(task_id)
        try:
            if path.is_symlink():
                raise ValueError("record is a symlink")
            record = TaskRecord(**json.loads(path.read_text()))
            expected = self.main / wt.WORKTREES_DIR / f"task-{task_id}"
            if (
                record.task_id != task_id
                or record.branch != f"task-{task_id}"
                or record.worktree != str(expected.resolve())
                or expected.is_symlink()
                or not Path(record.parent).is_absolute()
                or record.status not in _STATUSES
                or not re.fullmatch(r"[0-9a-f]{40,64}", record.base_commit)
                or (
                    record.head_commit is not None
                    and not re.fullmatch(r"[0-9a-f]{40,64}", record.head_commit)
                )
                or wt._git(
                    self.main, "check-ref-format", f"refs/heads/{record.parent_branch}", check=False
                ).returncode
            ):
                raise ValueError("invalid record paths, refs or status")
            return record
        except (OSError, TypeError, ValueError) as error:
            raise wt.WorktreeError(
                f"Cannot read task {task_id}: {error}; inspect {path}"
            ) from error

    def _save(self, record: TaskRecord) -> None:
        path = self._path(record.task_id)
        fd, temporary = tempfile.mkstemp(prefix=".task-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(asdict(record), file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _owned(self, task_id: str) -> TaskRecord:
        record = self._read(task_id)
        if record.parent != str(self.parent):
            raise wt.WorktreeError(
                f"Task {task_id} belongs to parent {record.parent}, not {self.parent}"
            )
        if record.status == "running" and not _alive(record):
            record.status = "failed"
            record.summary = (
                record.summary + "\nWorker interrupted: owning process exited; "
                "partial work is preserved. Inspect before discarding."
            ).strip()
            self._observe(record)
            self._save(record)
        return record

    def _observe(self, record: TaskRecord) -> None:
        path = Path(record.worktree)
        # Observe these independently: even a missing HEAD must not hide dirty files.
        errors = []
        for field, operation in (("head_commit", _head), ("dirty", _dirty)):
            try:
                setattr(record, field, operation(path))
            except (wt.WorktreeError, OSError) as error:
                errors.append(str(error))
        try:
            if _branch(path) != record.branch:
                errors.append("Worker changed branch; restore " + record.branch)
        except (wt.WorktreeError, OSError) as error:
            errors.append(str(error))
        if errors:
            record.status = "failed"
            record.summary += "\n" + "; ".join(errors)
            record.dirty = True  # unknown is never safe for automatic deletion

    def create(self, *, wait: bool = False) -> TaskRecord:
        with self._lock(wait=wait):
            # A manager may outlive its checkout. Recheck under the same lock used
            # by generic removal, before writing any record or creating a child.
            if not self.parent.is_dir():
                raise wt.WorktreeError(f"Task parent {self.parent} no longer exists")
            root = Path(wt._git(self.parent, "rev-parse", "--show-toplevel").stdout.strip())
            if root.resolve() != self.parent or wt.main_checkout(self.parent) != self.main:
                raise wt.WorktreeError(f"Task parent {self.parent} is no longer its checkout root")
            branch = _branch(self.parent)
            _idle(self.parent)
            if _dirty(self.parent, tracked=True):
                raise wt.WorktreeError(
                    "Parent has tracked changes; commit them before starting a task"
                )
            base = _head(self.parent)
            task_id = uuid.uuid4().hex
            name = f"task-{task_id}"
            record = TaskRecord(
                task_id,
                str(self.parent),
                branch,
                base,
                base,
                name,
                str(self.main / wt.WORKTREES_DIR / name),
                "running",
                False,
                pid=os.getpid(),
                process_started=psutil.Process().create_time(),
            )
            self._save(record)  # also protects an interrupted worktree add
            try:
                wt.create(self.parent, name, base=base)
            except Exception as error:
                record.status = "failed"
                record.summary = f"Worktree creation failed: {error}; inspect before discarding"
                self._save(record)
                raise
            return record

    def finish(
        self, task_id: str, status: str, summary: str = "", *, wait: bool = False
    ) -> TaskRecord:
        """Persist a stopped worker's outcome; the caller must first await its exit.

        Use wait=True for terminal persistence under contention. Async runtimes
        must run this in asyncio.to_thread and shield/join that thread on cancellation,
        so neither the event loop nor the terminal record is abandoned.
        """
        if status not in {"completed", "failed", "cancelled"}:
            raise wt.WorktreeError("Finish status must be completed, failed or cancelled")
        with self._lock(wait=wait):
            record = self._owned(task_id)
            if record.status == "running" and record.pid != os.getpid():
                raise wt.WorktreeError("Task worker belongs to another active process")
            if record.status in {"integrated", "discarded", "conflicted"}:
                raise wt.WorktreeError(
                    f"Task is {record.status}; inspect it rather than finishing again"
                )
            record.status, record.summary = status, summary
            self._observe(record)
            self._save(record)
            return record

    def get(self, task_id: str) -> TaskRecord:
        with self._lock():
            return self._owned(task_id)

    def list(self) -> list[TaskRecord]:
        with self._lock():
            return [
                self._owned(path.stem)
                for path in sorted(self.directory.glob("*.json"))
                if self._read(path.stem).parent == str(self.parent)
            ]

    def integrate(self, task_id: str) -> TaskRecord:
        with self._lock():
            record = self._owned(task_id)
            if _branch(self.parent) != record.parent_branch:
                raise wt.WorktreeError(
                    f"Parent changed branch; restore {record.parent_branch} first"
                )
            if record.status not in {"completed", "conflicted", "integrated"}:
                raise wt.WorktreeError(
                    f"Task is {record.status}; only successful committed results integrate"
                )
            child = Path(record.worktree)
            for path in (self.parent, child):
                _idle(path)
                if _dirty(path):
                    raise wt.WorktreeError(
                        f"{path} is dirty (including untracked files); clean it first"
                    )
            if _branch(child) != record.branch:
                raise wt.WorktreeError(f"Worker changed branch; restore {record.branch} first")
            if _head(child) != record.head_commit:
                raise wt.WorktreeError(
                    "Worker HEAD changed since finish; inspect and finish the task again"
                )
            if not record.head_commit or not _ancestor(
                self.parent, record.base_commit, record.head_commit
            ):
                raise wt.WorktreeError(
                    "Task result does not descend from its recorded base; inspect its history"
                )
            if not _ancestor(self.parent, record.base_commit, "HEAD"):
                raise wt.WorktreeError(
                    "Parent no longer descends from the task base; restore its history"
                )
            if not _ancestor(self.parent, record.head_commit, "HEAD"):
                result = wt._git(
                    self.parent,
                    "merge",
                    "--no-squash",
                    "--commit",
                    "--no-edit",
                    record.head_commit,
                    check=False,
                )
                if result.returncode:
                    record.status = "conflicted"
                    self._save(record)
                    raise wt.WorktreeError(
                        f"Task integration failed in parent {self.parent}: "
                        f"{(result.stderr or result.stdout).strip()}. Resolve and commit there, "
                        "then retry integration; the child is preserved."
                    )
            # Exit zero alone also describes a squash or an uncommitted merge.
            # Never publish integration until Git is idle and HEAD contains the result.
            try:
                _idle(self.parent)
                if _dirty(self.parent) or not _ancestor(self.parent, record.head_commit, "HEAD"):
                    raise wt.WorktreeError(
                        "Parent HEAD does not contain a clean committed task result"
                    )
            except wt.WorktreeError as error:
                record.status = "conflicted"
                self._save(record)
                raise wt.WorktreeError(
                    f"Task integration is incomplete in parent {self.parent}: {error}. "
                    "Finish or abort the merge there, then retry integration; "
                    "the child is preserved."
                ) from error
            record.status, record.dirty = "integrated", False
            self._save(record)
            return record

    def discard(self, task_id: str, confirm: bool = False) -> TaskRecord:
        with self._lock():
            record = self._owned(task_id)
            return self._discard(record, confirm)

    def _discard(self, record: TaskRecord, confirm: bool) -> TaskRecord:
        with parent_operation(Path(record.worktree), self.main):
            if record.status == "running":
                raise wt.WorktreeError(
                    "Task worker is active; cancel and await its completion first"
                )
            if record.status == "discarded":
                return record
            child = Path(record.worktree)
            if child.exists():
                if record.status == "integrated" and not confirm:
                    tree = wt.Worktree(child, record.branch, self.main)
                    if reason := keep_reason(tree):
                        raise wt.WorktreeError(reason)
                if _branch(child) != record.branch:
                    raise wt.WorktreeError(
                        f"Worker changed branch; restore {record.branch} before discard"
                    )
                if reason := children_reason(child):
                    raise wt.WorktreeError(reason)
                self._observe(record)
            safe = record.status == "integrated" and not record.dirty
            if safe and child.exists():
                safe = _head(child) == record.head_commit and _ancestor(
                    self.main, _head(child), f"refs/heads/{record.parent_branch}"
                )
            if not safe and not confirm:
                raise wt.WorktreeError(
                    "Task has unintegrated or dirty work; pass confirm=True to discard it"
                )
            if child.exists():
                wt._git(
                    self.main, "worktree", "remove", *(["--force"] if confirm else []), str(child)
                )
            # Git itself refuses deletion when the branch is checked out anywhere else.
            exists = wt._git(
                self.main,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{record.branch}",
                check=False,
            )
            if exists.returncode == 0:
                wt._git(self.main, "branch", "-D", record.branch)
            record.status = "discarded"
            self._save(record)
            return record


@contextmanager
def parent_operation(parent: Path, repo: Path):
    """Nonblocking lifecycle lock, stored in repo's shared Git directory.

    Resolve storage through a surviving checkout, not the potentially removed
    parent. Do not call a locking operation for the same parent inside this scope.
    """
    manager = TaskWorktrees(repo)
    manager.parent = parent.resolve()
    with manager._lock():
        yield


def discard_integrated(tree: wt.Worktree, record: TaskRecord) -> None:
    """Cleanup still works after the integrated task's parent checkout was removed."""
    manager = TaskWorktrees(tree.main)
    manager.parent = Path(record.parent)
    manager.discard(record.task_id)


def records(repo: Path) -> list[TaskRecord]:
    """Repository-wide metadata for generic lifecycle guards (including other parents)."""
    manager = TaskWorktrees(wt.main_checkout(repo) or repo)
    return [manager._read(path.stem) for path in sorted(manager.directory.glob("*.json"))]


def task_for(tree: wt.Worktree) -> TaskRecord | None:
    return next(
        (
            record
            for record in records(tree.main)
            if (record.worktree == str(tree.path.resolve()) or record.branch == tree.branch)
            and record.status != "discarded"
        ),
        None,
    )


def moved_reason(tree: wt.Worktree, record: TaskRecord) -> str:
    if record.worktree != str(tree.path.resolve()):
        return (
            f"task {record.task_id} moved to {tree.path}; restore it with "
            f"git worktree move {shlex.quote(str(tree.path))} {shlex.quote(record.worktree)} "
            "before task integration or removal"
        )
    return ""


def children_reason(parent: Path) -> str:
    for record in records(parent):
        if record.parent == str(parent.resolve()) and record.status not in {
            "integrated",
            "discarded",
        }:
            return (
                f"owns task {record.task_id} ({record.status}), "
                "awaiting integration or explicit discard"
            )
    return ""


def keep_reason(tree: wt.Worktree) -> str | None:
    """None means an ordinary worktree; empty means a safely integrated task."""
    if reason := children_reason(tree.path):
        return reason
    record = task_for(tree)
    if record is None:
        return None
    if reason := moved_reason(tree, record):
        return reason
    if record.status != "integrated":
        return f"task {record.task_id} ({record.status}), awaiting integration or explicit discard"
    try:
        _idle(tree.path)
        if _dirty(tree.path) or _branch(tree.path) != record.branch:
            return "integrated task has dirty files or a changed branch"
        if _head(tree.path) != record.head_commit or not _ancestor(
            tree.main, _head(tree.path), f"refs/heads/{record.parent_branch}"
        ):
            return "task result changed or is no longer integrated into its parent"
    except wt.WorktreeError as error:
        return str(error)
    return ""
