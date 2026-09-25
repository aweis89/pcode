"""Net git diffs for /diffs: what a session's work adds up to, one entry per file.

A linked worktree belongs to its session, so its whole branch is compared with
the merge-base on the mainline branch. That is exactly what a merge would bring
in, and it stays right after `/worktree merge` merges mainline into the branch,
where a remembered starting commit would claim mainline's changes too. A shared
checkout compares HEAD with only the files this session's tools edited, since
anything else dirty there may be the user's.

`git diff` never shows untracked files, and staging them would change the
user's index. So the working tree is recorded into a throwaway copy of the index
(`GIT_INDEX_FILE`) and diffed with `--cached`; the real index is never written.
The copy sits beside the real one because a split index finds its shared half
there. Untracked files that look sensitive are listed but never hashed, so their
contents do not reach the object store.
"""

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pcode import worktree
from pcode.edits import edit_text, sensitive_path
from pcode.runtime import EditCompleted
from pcode.tool_display import plain

# One file's raw diff beyond this is counted but not shown; a lockfile rewrite
# would otherwise stall redaction and the browser's search.
MAX_FILE_DIFF = 1024 * 1024
MAX_DIFF_LINES = 2000
OPERATIONS = {"A": "created", "C": "copied", "D": "deleted", "R": "renamed"}
# Pin everything user config can change about the output this module parses.
DIFF_OPTIONS = (
    "--cached",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-relative",
    "--find-renames",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


class GitDiffError(Exception):
    """Git could not produce the view; the caller falls back to the tool-edit log."""


@dataclass(frozen=True)
class DiffView:
    title: str
    changes: list[EditCompleted]
    empty: str


def session_diff(workspace: Path, edited: Iterable[str]) -> DiffView | None:
    """The git view of `workspace`, or None outside a git repository.

    `edited` holds the paths this session's file tools changed, relative to the
    workspace; only a shared checkout uses them.
    """
    try:
        if worktree.project_checkout(workspace) is None:
            return None
        linked = worktree.describe(workspace)
        if linked is not None:
            return branch_diff(linked)
        return edited_files_diff(workspace, edited)
    except worktree.WorktreeError as error:
        raise GitDiffError(str(error)) from error


def branch_diff(linked: worktree.Worktree) -> DiffView:
    mainline = worktree.mainline_branch(linked.main)
    base = _git(linked.path, "merge-base", "HEAD", mainline).decode().strip()
    branch, mainline = plain(linked.branch), plain(mainline)
    return DiffView(
        f"Git diff · {branch} vs {mainline} (merge-base {_short(linked.path, base)})"
        " · includes uncommitted and new files",
        _diff(linked.path, base),
        f"{branch} has nothing that {mainline} does not already have.",
    )


def edited_files_diff(workspace: Path, edited: Iterable[str]) -> DiffView:
    top = Path(_git(workspace, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if not _git(workspace, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False):
        raise GitDiffError("the repository has no commits yet")
    paths = set()
    for path in edited:
        # Tool paths are relative to the workspace, which may sit below the top level.
        location = Path(os.path.normpath(workspace / path))
        if location.is_relative_to(top):
            paths.add(location.relative_to(top).as_posix())
    return DiffView(
        f"Git diff · uncommitted changes vs HEAD ({_short(top, 'HEAD')})"
        " · only files edited this session",
        _diff(top, "HEAD", only=paths) if paths else [],
        "No uncommitted changes in files edited this session.",
    )


def _diff(top: Path, base: str, only: set[str] | None = None) -> list[EditCompleted]:
    """Working tree against `base`, including untracked files, one change per file."""
    index = Path(
        _git(top, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip()
    )
    descriptor, scratch = tempfile.mkstemp(prefix="pcode-diff-index-", dir=index.parent)
    os.close(descriptor)
    env = {
        **os.environ,
        "GIT_INDEX_FILE": scratch,
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        # Starting from the real index keeps staged work and its stat cache, so
        # only files that differ from it are hashed below. copy2 keeps the
        # index's mtime: git rechecks the content of any entry modified in the
        # same second the index was written, and a fresh mtime would make a
        # same-size edit from that second look unchanged.
        if index.exists():
            shutil.copy2(index, scratch)
        else:
            os.unlink(scratch)
        untracked = _paths(_git(top, "ls-files", "-z", "--others", "--exclude-standard", env=env))
        changed = _paths(_git(top, "ls-files", "-z", "--modified", "--deleted", env=env))
        if only is not None:
            untracked &= only
            changed &= only
        secret = {path for path in untracked if sensitive_path(path)}
        stage = sorted(changed | (untracked - secret))
        if stage:
            listing = b"\0".join(path.encode("utf-8", "surrogateescape") for path in stage)
            _git(
                top,
                "add",
                "--all",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                env=env,
                input=listing,
            )
        names = _git(top, "diff", *DIFF_OPTIONS, "--name-status", "-z", base, env=env)
        patch = _git(top, "diff", *DIFF_OPTIONS, "--patch", base, env=env)
    finally:
        Path(scratch).unlink(missing_ok=True)
    changes = [
        _change(status, old, new, chunk)
        for (status, old, new), chunk in _pair(_entries(names), patch)
        if only is None or old in only or new in only
    ]
    changes.extend(
        EditCompleted("", plain(path, limit=None), "created", omitted="Sensitive file; not read")
        for path in secret
    )
    return sorted(changes, key=lambda change: change.path)


def _pair(entries: list[tuple[str, str, str]], patch: bytes):
    """Match each name-status entry with its patch text, both in git's file order."""
    chunks = iter(chunk for chunk in re.split(rb"^(?=diff --git )", patch, flags=re.M) if chunk)
    paired = []
    for entry in entries:
        # A type change (file to symlink) is printed as a deletion and a creation.
        taken = [next(chunks, None) for _ in range(2 if entry[0] == "T" else 1)]
        if None in taken:
            raise GitDiffError("git listed more files than patches")
        paired.append((entry, b"".join(taken)))
    if next(chunks, None) is not None:
        raise GitDiffError("git listed more patches than files")
    return paired


def _change(status: str, old: str, new: str, chunk: bytes) -> EditCompleted:
    path = plain(edit_text(new if old == new else f"{old} → {new}"), limit=None)
    operation = OPERATIONS.get(status, "edited")
    lines = chunk.decode("utf-8", "replace").splitlines()
    added, removed = _counts(lines)
    if sensitive_path(old) or sensitive_path(new):
        return EditCompleted("", path, operation, "", added, removed, omitted="Sensitive file")
    if len(chunk) > MAX_FILE_DIFF:
        return EditCompleted(
            "", path, operation, "", added, removed, omitted="Diff exceeds preview size limit"
        )
    # `diff --git` repeats the path and `index` only names blobs. Hunk lines
    # always carry a prefix, so neither can be file content.
    body = [line for line in lines if not line.startswith(("diff --git ", "index "))]
    shown = edit_text("\n".join(body)).splitlines()
    truncated = len(shown) > MAX_DIFF_LINES
    patch = "\n".join(shown[:MAX_DIFF_LINES])
    return EditCompleted("", path, operation, patch, added, removed, truncated)


def _counts(lines: list[str]) -> tuple[int, int]:
    """Changed lines inside hunks; the `---`/`+++` headers come before the first."""
    added = removed = 0
    in_hunk = False
    for line in lines:
        if line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            added += 1
        elif in_hunk and line.startswith("-"):
            removed += 1
    return added, removed


def _entries(raw: bytes) -> list[tuple[str, str, str]]:
    """`--name-status -z` records as (status, old path, new path)."""
    fields = raw.split(b"\0")
    entries = []
    position = 0
    while position < len(fields) and fields[position]:
        status = fields[position].decode()[0]
        if status in "RC":
            old, new = fields[position + 1], fields[position + 2]
            position += 3
        else:
            old = new = fields[position + 1]
            position += 2
        entries.append((status, _decode(old), _decode(new)))
    return entries


def _paths(raw: bytes) -> set[str]:
    return {_decode(path) for path in raw.split(b"\0") if path}


def _decode(path: bytes) -> str:
    # Surrogate escapes round-trip a non-UTF-8 name back to git as a pathspec.
    return path.decode("utf-8", "surrogateescape")


def _short(top: Path, revision: str) -> str:
    return _git(top, "rev-parse", "--short", revision).decode().strip()


def _git(
    cwd: Path, *args: str, env: dict | None = None, input: bytes | None = None, check: bool = True
) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "-c", "core.quotePath=false", *args],
            capture_output=True,
            env=env,
            input=input,
            check=False,
        )
    except OSError as error:
        raise GitDiffError(f"git could not run: {error}") from error
    if result.returncode:
        if not check:
            return b""
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit {result.returncode}"
        raise GitDiffError(f"git {args[0]} failed: {detail}")
    return result.stdout
