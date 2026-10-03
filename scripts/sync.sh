#!/bin/sh
# Bring this clone in line with origin and set up its hooks and identity.
#
#   make sync                 # from a clone that has this script
#   git fetch origin && git show origin/master:scripts/sync.sh | sh
#                             # from an older clone that doesn't yet
#
# Safe to run any time. It:
#  1. points core.hooksPath at .githooks and sets this clone's user.email,
#     which .githooks/pre-commit enforces (EMAIL=... overrides; otherwise an
#     existing local value, else the newest author on the mainline);
#  2. fetches, then moves the local mainline to origin's. A fast-forward is
#     done as is. A mainline whose own commits all exist upstream under other
#     ids (history rewritten on origin) is reset to origin, which a plain
#     `git pull` must never do instead: it would merge the old commits back in
#     and the post-merge hook would push them. Anything else is left alone;
#  3. lists local branches still carrying commits from before such a rewrite,
#     which `git rebase <mainline>` fixes by dropping the old copies.
set -eu

cd "$(git rev-parse --show-toplevel)"
main_tree=$(cd "$(git rev-parse --git-common-dir)/.." && pwd)

git config core.hooksPath .githooks
git fetch --quiet --prune origin
upstream=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/master)
branch=${upstream#origin/}

email=${EMAIL:-$(git config --local user.email || git log -1 --format=%ae "$upstream")}
git config --local user.email "$email"
echo "identity: user.email=$email (commits under any other address are refused; EMAIL=... to change)"

# Commits on $1 that $upstream lacks even by content ("+" lines of git cherry).
own() { git cherry "$upstream" "$1" | grep -c '^+' || true; }
# Commits on $1 that $upstream has only under another id ("-" lines).
stale() { git cherry "$upstream" "$1" | grep -c '^-' || true; }

local_head=$(git rev-parse --verify --quiet "refs/heads/$branch" || true)
checked_out=$(git -C "$main_tree" symbolic-ref --quiet --short HEAD || true)
if [ -z "$local_head" ]; then
	echo "$branch: no local branch; nothing to update"
elif git merge-base --is-ancestor "$local_head" "$upstream"; then
	if [ "$local_head" = "$(git rev-parse "$upstream")" ]; then
		echo "$branch: up to date"
	elif [ "$checked_out" = "$branch" ]; then
		git -C "$main_tree" merge --quiet --ff-only "$upstream"
		echo "$branch: fast-forwarded"
	else
		git update-ref "refs/heads/$branch" "$upstream" "$local_head"
		echo "$branch: fast-forwarded"
	fi
elif [ "$(own "$local_head")" -eq 0 ]; then
	# Rewritten upstream: everything here exists there under new ids.
	if [ "$checked_out" = "$branch" ]; then
		git -C "$main_tree" reset --quiet --keep "$upstream"
	else
		git update-ref "refs/heads/$branch" "$upstream" "$local_head"
	fi
	echo "$branch: reset to $upstream (history was rewritten upstream; was $(git rev-parse --short "$local_head"))"
else
	echo "$branch: has commits $upstream lacks; left alone. Inspect with: git cherry -v $upstream $branch" >&2
fi

git for-each-ref --format='%(refname:short)' refs/heads | while read -r b; do
	[ "$b" = "$branch" ] && continue
	[ "$(stale "$b")" -gt 0 ] || continue
	where=$(git worktree list --porcelain | awk -v ref="branch refs/heads/$b" '/^worktree /{w=substr($0,10)} $0==ref{print w}')
	mine=$(own "$b")
	if [ "$mine" -eq 0 ]; then
		echo "$b: on old history, but $upstream already has all of it; safe to delete${where:+ (worktree $where)}" >&2
	else
		echo "$b: $mine commit(s) of its own on old history; run: git ${where:+-C $where }rebase $upstream" >&2
	fi
done
