"""Throwaway git worktrees, one per session, so concurrent sessions never share a tree.

The workspace *is* the worktree: file tools, the shell, repo context, and the
saved session all point at it, so the model needs no instructions and relative
paths cannot land in the mainline checkout by mistake.

Setup that git cannot do (installing dependencies, copying untracked config,
symlinking shared caches) lives in two optional scripts run after checkout:
`~/.config/pcode/worktree-setup` for every repo, then `<repo>/.pcode/worktree-setup`
for this one. The project script is arbitrary code shipped with the repo, so it
only runs for a trusted repository, like `.pcode/extensions`.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pcode.preferences import preferences_path

WORKTREES_DIR = ".worktrees"
SETUP_SCRIPT = "worktree-setup"
PROJECT_SETUP = Path(".pcode") / SETUP_SCRIPT


class WorktreeError(ValueError):
    """A ValueError so slash-command dispatch reports it as a plain message."""


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    main: Path


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise WorktreeError(f"git {args[0]} failed: {detail}")
    return result


def main_checkout(path: Path) -> Path | None:
    """The repository's primary worktree, or None when `path` is not in a git repo."""
    result = _git(path, "worktree", "list", "--porcelain", check=False)
    if result.returncode:
        return None
    first = result.stdout.splitlines()[0]
    return Path(first.removeprefix("worktree ")).resolve()


def is_linked(path: Path) -> bool:
    """True inside a secondary worktree (created by `git worktree add`)."""
    main = main_checkout(path)
    if main is None:
        return False
    toplevel = _git(path, "rev-parse", "--show-toplevel").stdout.strip()
    return Path(toplevel).resolve() != main


def mainline_branch(main: Path) -> str:
    result = _git(main, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if result.returncode:
        raise WorktreeError(f"mainline checkout {main} is not on a branch")
    return result.stdout.strip()


def validate_name(name: str) -> str:
    if not name or "/" in name or name.startswith(".") or any(c.isspace() for c in name):
        raise WorktreeError(f"worktree name must be a plain name, got {name!r}")
    return name


def create(repo: Path, name: str, base: str | None = None) -> Worktree:
    """Add `.worktrees/<name>` on branch `<name>`, reusing the branch if it exists."""
    validate_name(name)
    main = main_checkout(repo)
    if main is None:
        raise WorktreeError(f"{repo} is not inside a git repository")
    path = main / WORKTREES_DIR / name
    if path.exists():
        raise WorktreeError(f"{path} already exists")
    base = base or mainline_branch(main)
    path.parent.mkdir(exist_ok=True)
    _exclude_worktrees_dir(main)
    exists = _git(main, "show-ref", "--quiet", "--verify", f"refs/heads/{name}", check=False)
    if exists.returncode == 0:
        _git(main, "worktree", "add", str(path), name)
    else:
        _git(main, "worktree", "add", "-b", name, str(path), base)
    return Worktree(path=path, branch=name, main=main)


def _exclude_worktrees_dir(main: Path) -> None:
    """Keep `.worktrees/` out of `git status` without touching the tracked .gitignore."""
    common = _git(main, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout
    exclude = Path(common.strip()) / "info" / "exclude"
    entry = f"/{WORKTREES_DIR}/"
    try:
        lines = exclude.read_text().splitlines() if exclude.exists() else []
        if entry not in lines and f"{WORKTREES_DIR}/" not in lines:
            exclude.parent.mkdir(exist_ok=True)
            with exclude.open("a") as file:
                file.write(f"{entry}\n")
    except OSError:
        pass  # cosmetic; git status noise is not worth failing creation


def describe(path: Path) -> Worktree | None:
    """The worktree `path` lives in, or None when it is the mainline or not a repo."""
    main = main_checkout(path)
    if main is None:
        return None
    toplevel = Path(_git(path, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if toplevel == main:
        return None
    branch = _git(toplevel, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return Worktree(path=toplevel, branch=branch.stdout.strip() or "(detached)", main=main)


def setup_scripts(worktree: Worktree, project: bool | None = None) -> list[Path]:
    """Existing setup scripts in run order: user-level first, then the project's.

    The project script is taken from the new checkout when its branch has one
    (so it matches the code being set up), else from the mainline, where an
    untracked local copy may live. `project` None defers to whether the
    repository is trusted; True runs it regardless, for an explicit invocation
    inside that repo.
    """
    from pcode.project_trust import is_trusted

    scripts = [preferences_path().parent / SETUP_SCRIPT]
    if project is None:
        project = is_trusted(worktree.main)
    if project:
        scripts.append(
            next(
                (
                    p
                    for p in (worktree.path / PROJECT_SETUP, worktree.main / PROJECT_SETUP)
                    if p.is_file()
                ),
                worktree.main / PROJECT_SETUP,
            )
        )
    return [script for script in scripts if script.is_file()]


def run_setup(worktree: Worktree, stream=None, project: bool | None = None) -> None:
    """Run each setup script inside the worktree, echoing its output to `stream`.

    Executable scripts run directly (so they need a shebang, like git hooks);
    anything else goes through `sh`. A failing script aborts creation.
    """
    env = {
        **os.environ,
        "PCODE_MAIN": str(worktree.main),
        "PCODE_WORKTREE": str(worktree.path),
        "PCODE_BRANCH": worktree.branch,
    }
    for script in setup_scripts(worktree, project):
        if stream is not None:
            print(f"worktree: running {script}", file=stream)
        command = [str(script)] if os.access(script, os.X_OK) else ["sh", str(script)]
        result = subprocess.run(
            command,
            cwd=worktree.path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if stream is not None and result.stdout:
            stream.write(result.stdout)
        if result.returncode:
            raise WorktreeError(f"{script} exited {result.returncode}")


def is_dirty(path: Path) -> bool:
    return bool(_git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip())


def unmerged_commits(worktree: Worktree) -> int:
    """Commits on the worktree's branch that the mainline branch does not have."""
    mainline = mainline_branch(worktree.main)
    result = _git(worktree.path, "rev-list", "--count", f"{mainline}..HEAD", check=False)
    return int(result.stdout.strip() or 0) if result.returncode == 0 else 0


def merge(worktree: Worktree) -> str:
    """Merge the mainline branch into the worktree, then fast-forward the mainline.

    Conflicts are resolved inside the worktree so the mainline checkout is
    never left mid-merge; it only ever moves by fast-forward.
    """
    if is_dirty(worktree.path):
        raise WorktreeError(f"{worktree.path} has uncommitted changes; commit them first")
    mainline = mainline_branch(worktree.main)
    result = _git(worktree.path, "merge", "--no-edit", mainline, check=False)
    if result.returncode:
        raise WorktreeError(
            f"conflicts merging {mainline} into {worktree.branch}: "
            f"resolve them in {worktree.path}, commit, then run /worktree merge again"
        )
    result = _git(worktree.main, "merge", "--ff-only", worktree.branch, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise WorktreeError(
            f"could not fast-forward {mainline} in {worktree.main}: {detail}\n"
            "Commit or stash those mainline changes, or re-run if the branch moved."
        )
    return f"merged {worktree.branch} into {mainline}"


def remove(worktree: Worktree, force: bool = False) -> str:
    """Delete the worktree directory, keeping its branch.

    `force` is for a worktree pcode itself just created and abandoned; user
    work is never forced because another session may still be in there.
    """
    flags = ["--force"] if force else []
    result = _git(worktree.main, "worktree", "remove", *flags, str(worktree.path), check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise WorktreeError(
            f"not removing {worktree.path}: {detail}\n"
            f"If that work is disposable: git worktree remove --force {worktree.path}"
        )
    return f"removed {worktree.path}"


def is_untouched(worktree: Worktree) -> bool:
    """Nothing to lose: no tracked changes, no untracked files, nothing unmerged.

    Ignored files (a `.venv`, the shared `tmp` symlink) do not count, matching
    what a non-forced `git worktree remove` tolerates.
    """
    status = _git(worktree.path, "status", "--porcelain").stdout.strip()
    return not status and unmerged_commits(worktree) == 0


def delete_branch(worktree: Worktree) -> None:
    """Drop a fully merged branch; `-d` refuses anything unmerged, which is the point."""
    _git(worktree.main, "branch", "-d", worktree.branch, check=False)


def finish(worktree: Worktree) -> str:
    """Merge into the mainline, then remove the worktree and its branch.

    Any refusal (dirty tree, conflicts, blocked fast-forward, untracked files)
    raises before anything is deleted, leaving the worktree resumable.
    """
    if is_dirty(worktree.path):
        raise WorktreeError(f"{worktree.path} has uncommitted changes; commit them first")
    merged = merge(worktree) if unmerged_commits(worktree) else None
    removed = remove(worktree)
    delete_branch(worktree)
    return f"{merged}; {removed}" if merged else removed


def listing(repo: Path) -> str:
    return _git(repo, "worktree", "list").stdout.rstrip()


def main(argv: list[str] | None = None) -> int:
    """`python -m pcode.worktree new|merge|remove|list [NAME]`, for Makefiles and humans.

    Same operations as `pcode --worktree` and `/worktree`, without a session.
    The project's setup script runs unconditionally here: invoking this inside
    the repo is the consent that `project_extensions` stands in for otherwise.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m pcode.worktree")
    parser.add_argument("command", choices=("new", "merge", "remove", "list"))
    parser.add_argument("name", nargs="?")
    parser.add_argument("--base", help="branch or commit to start from (new only)")
    args = parser.parse_args(argv)
    cwd = Path.cwd()
    try:
        if args.command == "list":
            print(listing(cwd))
            return 0
        if not args.name:
            parser.error(f"{args.command} needs a NAME")
        if args.command == "new":
            created = create(cwd, args.name, args.base)
            try:
                run_setup(created, stream=sys.stderr, project=True)
            except WorktreeError:
                remove(created, force=True)
                raise
            print(f"worktree ready: {created.path} (branch {created.branch})")
            return 0
        main_path = main_checkout(cwd)
        if main_path is None:
            raise WorktreeError(f"{cwd} is not inside a git repository")
        target = describe(main_path / WORKTREES_DIR / args.name)
        if target is None:
            raise WorktreeError(f"no worktree named {args.name} under {main_path / WORKTREES_DIR}")
        print(merge(target) if args.command == "merge" else remove(target))
        return 0
    except WorktreeError as error:
        print(f"worktree: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
