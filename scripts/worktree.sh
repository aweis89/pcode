#!/usr/bin/env bash
# Manage throwaway git worktrees under .worktrees/ so several agents can edit
# the same checkout without fighting over the working tree.
#
# Each worktree gets its own .venv: uv clones it from the local cache in a
# couple of seconds, and the editable install records an absolute path to
# src/, so a shared .venv would silently import the other checkout's source.
# Only genuinely shared, path-independent state is symlinked (tmp/).
set -euo pipefail

MAIN="$(git worktree list --porcelain | head -n1 | cut -d' ' -f2-)"
WORKTREES="$MAIN/.worktrees"
SHARED=(tmp)

die() {
	echo "worktree: $*" >&2
	exit 1
}

usage() {
	cat <<'EOF'
usage: scripts/worktree.sh <command> [args]

  new <name> [base]   Create .worktrees/<name> on branch <name> (default base: mainline)
  merge <name>        Merge mainline into the worktree, then fast-forward mainline
  remove <name>       Delete the worktree (its branch is kept)
  list                Show every worktree
EOF
}

mainline() {
	git -C "$MAIN" symbolic-ref --quiet --short HEAD \
		|| die "mainline checkout $MAIN is not on a branch"
}

worktree_path() {
	[ -n "${1:-}" ] || die "missing worktree name"
	case "$1" in
	*/* | .*) die "worktree name must be a plain name, got '$1'" ;;
	esac
	echo "$WORKTREES/$1"
}

link_shared() {
	local dir="$1" name
	for name in "${SHARED[@]}"; do
		mkdir -p "$MAIN/$name"
		ln -sfn "$MAIN/$name" "$dir/$name"
	done
}

cmd_new() {
	local name="${1:-}" base="${2:-}" dir
	dir="$(worktree_path "$name")"
	base="${base:-$(mainline)}"
	if [ -e "$dir" ]; then
		die "$dir already exists (use 'remove $name' first)"
	fi

	mkdir -p "$WORKTREES"
	if git -C "$MAIN" show-ref --quiet --verify "refs/heads/$name"; then
		git -C "$MAIN" worktree add "$dir" "$name"
	else
		git -C "$MAIN" worktree add -b "$name" "$dir" "$base"
	fi

	link_shared "$dir"
	(cd "$dir" && uv sync --quiet)

	echo
	echo "worktree ready: $dir (branch $name, from $base)"
	echo "  cd $dir && make test"
}

cmd_merge() {
	local name="${1:-}" dir main_branch
	dir="$(worktree_path "$name")"
	[ -d "$dir" ] || die "no worktree at $dir"
	main_branch="$(mainline)"

	# Untracked files are fine; git's own merge refuses if one is in the way.
	if [ -n "$(git -C "$dir" status --porcelain --untracked-files=no)" ]; then
		die "$dir has uncommitted changes; commit them first"
	fi

	# Resolve conflicts inside the worktree so the mainline merge is a
	# fast-forward and the mainline working tree is never left conflicted.
	if ! git -C "$dir" merge --no-edit "$main_branch"; then
		echo
		die "conflicts merging $main_branch into $name: fix them in $dir, commit, then re-run"
	fi

	if ! git -C "$MAIN" merge --ff-only "$name"; then
		echo
		die "could not fast-forward $main_branch in $MAIN: commit or stash the changes git named above (or, if $main_branch moved again, just re-run this merge)"
	fi

	echo
	echo "merged $name into $main_branch; remove the worktree with: scripts/worktree.sh remove $name"
}

cmd_remove() {
	local name="${1:-}" dir
	dir="$(worktree_path "$name")"
	[ -d "$dir" ] || die "no worktree at $dir"
	git -C "$MAIN" worktree remove --force "$dir"
	echo "removed $dir (branch $name kept)"
}

case "${1:-}" in
new) shift && cmd_new "$@" ;;
merge) shift && cmd_merge "$@" ;;
remove) shift && cmd_remove "$@" ;;
list) git -C "$MAIN" worktree list ;;
"" | -h | --help | help) usage ;;
*) die "unknown command '$1' (try --help)" ;;
esac
