"""Net git diffs for /diffs: what a session's work adds up to, one entry per file.

A linked worktree belongs to its session, so its whole branch is compared with
the merge-base on the mainline branch. That is exactly what a merge would bring
in, and it stays right after `/worktree merge` merges mainline into the branch,
where a remembered starting commit would claim mainline's changes too. A shared
checkout has no branch of its own, so it compares the working tree with the
commit the session started from, but only for files this session's tools edited
or its commits touched, since anything else there may be the user's. A commit
counts as the session's when it is new since the start, was made after the
session began, and by this checkout's git identity; a pulled commit is neither.
Each view also has a companion showing only what is not yet committed.

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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
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


def session_views(
    workspace: Path,
    edited: Iterable[str],
    start: str | None = None,
    since: str | None = None,
) -> list[Callable[[], DiffView]]:
    """Loaders for the git views of `workspace`, the session's net work first.

    Empty outside a git repository. `edited` holds the paths this session's
    file tools changed, relative to the workspace; `start` is the commit the
    session began at and `since` when (ISO 8601). Only a shared checkout uses
    them. Each loader runs git and may raise GitDiffError.
    """
    try:
        if worktree.project_checkout(workspace) is None:
            return []
        linked = worktree.describe(workspace)
    except worktree.WorktreeError as error:
        raise GitDiffError(str(error)) from error
    if linked is not None:
        return [partial(branch_diff, linked), partial(uncommitted_diff, linked.path)]
    edited = list(edited)
    return [
        partial(edited_files_diff, workspace, edited, start, since),
        partial(uncommitted_diff, workspace, edited, start, since, shared=True),
    ]


def session_diff(
    workspace: Path, edited: Iterable[str], start: str | None = None, since: str | None = None
) -> DiffView | None:
    """The session's net git view of `workspace`, or None outside a git repository."""
    views = session_views(workspace, edited, start, since)
    return views[0]() if views else None


def branch_diff(linked: worktree.Worktree) -> DiffView:
    try:
        mainline = worktree.mainline_branch(linked.main)
    except worktree.WorktreeError as error:
        raise GitDiffError(str(error)) from error
    base = _git(linked.path, "merge-base", "HEAD", mainline).decode().strip()
    branch, mainline = plain(linked.branch), plain(mainline)
    return DiffView(
        f"Git diff · {branch} vs {mainline} (merge-base {_short(linked.path, base)})"
        " · includes uncommitted and new files",
        _diff(linked.path, base),
        f"{branch} has nothing that {mainline} does not already have.",
    )


def edited_files_diff(
    workspace: Path, edited: Iterable[str], start: str | None = None, since: str | None = None
) -> DiffView:
    """A shared checkout's session work: the working tree against the starting commit.

    Falls back to uncommitted changes against HEAD, in edited files only, when
    no start was recorded or HEAD no longer descends from it (a rebase, a
    branch switch).
    """
    top = _toplevel(workspace)
    edited_paths = _top_relative(workspace, top, edited)
    if start is None or not _is_ancestor(top, start):
        why = "no starting commit recorded" if start is None else "HEAD moved off the start"
        return DiffView(
            f"Git diff · uncommitted changes vs HEAD ({_short(top, 'HEAD')})"
            f" · only files edited this session · {why}",
            _diff(top, "HEAD", only=edited_paths) if edited_paths else [],
            "No uncommitted changes in files edited this session.",
        )
    paths = edited_paths | _committed(top, start, since)
    return DiffView(
        f"Git diff · since the session started ({_short(top, start)})"
        " · files this session edited or committed, including uncommitted and new files",
        _diff(top, start, only=paths) if paths else [],
        "No changes since the session started in files it edited or committed.",
    )


def uncommitted_diff(
    workspace: Path,
    edited: Iterable[str] = (),
    start: str | None = None,
    since: str | None = None,
    *,
    shared: bool = False,
) -> DiffView:
    """What the next commit would take in; a shared checkout limits it to session files."""
    top = _toplevel(workspace)
    short = _short(top, "HEAD")
    if not shared:
        return DiffView(
            f"Git diff · uncommitted changes vs HEAD ({short}) · including new files",
            _diff(top, "HEAD"),
            "No uncommitted changes.",
        )
    paths = _top_relative(workspace, top, edited)
    if start is not None and _is_ancestor(top, start):
        paths |= _committed(top, start, since)
    return DiffView(
        f"Git diff · uncommitted changes vs HEAD ({short}) · only files this session touched",
        _diff(top, "HEAD", only=paths) if paths else [],
        "No uncommitted changes in files this session touched.",
    )


def _toplevel(workspace: Path) -> Path:
    top = Path(_git(workspace, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if not _git(workspace, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False):
        raise GitDiffError("the repository has no commits yet")
    return top


def _top_relative(workspace: Path, top: Path, edited: Iterable[str]) -> set[str]:
    paths = set()
    for path in edited:
        # Tool paths are relative to the workspace, which may sit below the top level.
        location = Path(os.path.normpath(workspace / path))
        if location.is_relative_to(top):
            paths.add(location.relative_to(top).as_posix())
    return paths


def _is_ancestor(top: Path, start: str) -> bool:
    """Whether HEAD still descends from `start`; a commit git lost is not an ancestor."""
    # The hash comes from session.json; never let it reach git as an option.
    if not re.fullmatch(r"[0-9a-f]{40,64}", start):
        return False
    try:
        _git(top, "merge-base", "--is-ancestor", start, "HEAD")
    except GitDiffError:
        return False
    return True


def _committed(top: Path, start: str, since: str | None) -> set[str]:
    """Paths touched by the session's own commits after `start`."""
    args = ["log", "--format=", "--name-only", "-z", "--no-renames", "--no-merges"]
    args.append("--no-show-signature")
    if since:
        args.append(f"--since={since}")
    email = _git(top, "config", "user.email", check=False).decode().strip()
    if email:
        args += ["--fixed-strings", f"--committer=<{email}>"]
    return _paths(_git(top, *args, f"{start}..HEAD", "--"))


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
        # Limit git itself, not just the result: against a start commit, a pull
        # would otherwise patch every file it brought in. Literal pathspecs
        # (set above); a rename with one side outside `only` shows as one side.
        paths = ["--", *sorted(only)] if only is not None else []
        names = _git(top, "diff", *DIFF_OPTIONS, "--name-status", "-z", base, *paths, env=env)
        patch = _git(top, "diff", *DIFF_OPTIONS, "--patch", base, *paths, env=env)
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
