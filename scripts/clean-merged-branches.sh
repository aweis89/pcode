#!/bin/sh
# Delete branches the default branch already has, locally and on origin.
#
# Worktrees are not this script's job: git refuses `branch -d` for a branch
# checked out in one, so `make worktree-clean` has to run first (the
# clean-merged target sequences them). Branches still held by a worktree are
# skipped here rather than reported as failures.
#
# Pass --dry-run to print what would go without touching anything.
set -eu

dry_run=""
case "${1:-}" in
    --dry-run) dry_run=1 ;;
    "") ;;
    *)
        echo "usage: $0 [--dry-run]" >&2
        exit 2
        ;;
esac

run() {
    if [ -n "$dry_run" ]; then
        echo "would: $*"
    else
        "$@"
    fi
}

git fetch origin --prune

# The symbolic ref is local and free; `git remote show` is a second network
# round trip, so it is only the fallback for a clone that never set HEAD.
default=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##') || true
if [ -z "$default" ]; then
    default=$(git remote show origin | sed -n '/HEAD branch/s/.*: //p')
fi
if [ -z "$default" ] || ! git rev-parse --verify --quiet "origin/$default" >/dev/null; then
    echo "cannot resolve origin's default branch" >&2
    exit 1
fi
echo "default branch: $default"

# %(worktreepath) is empty unless some worktree has the branch checked out.
git branch --merged "origin/$default" --format='%(refname:short) %(worktreepath)' |
    awk -v skip="$default" '$2 == "" && $1 != skip {print $1}' |
    while IFS= read -r branch; do
        run git branch -d "$branch"
    done

git branch -r --merged "origin/$default" --format='%(refname:short)' |
    sed -n 's#^origin/##p' |
    grep -vxF "$default" |
    while IFS= read -r branch; do
        run git push origin --delete "$branch"
    done
