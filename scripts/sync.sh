#!/bin/sh
# Bring this clone in line with origin and set up its hooks and identity.
#
#   make sync                 # from a clone that has this script
#   git fetch origin && git show origin/master:scripts/sync.sh | sh
#                             # from an older clone that doesn't yet
#
# Safe to run any time. It:
#  0. repoints an origin still at the pre-transfer aweis89/pcode to
#     cruxwell/pcode, keeping its scheme (https or ssh);
#  1. points core.hooksPath at .githooks and sets this clone's user.email,
#     which the hooks enforce (SYNC_EMAIL=... overrides; otherwise an existing
#     local value, else the global one);
#  2. fetches, then moves the local mainline to origin's. A fast-forward is
#     done as is. A mainline whose own commits all exist upstream under other
#     ids (history rewritten on origin) is reset to origin, which a plain
#     `git pull` must never do instead: it would merge the old commits back in
#     and the post-merge hook would push them. Anything else is left alone;
#  3. lists local branches still on history origin has under other ids:
#     deletable when origin has all of it, else `git rebase <mainline>`.
set -eu

cd "$(git rev-parse --show-toplevel)"

url=$(git remote get-url origin)
new_url=$(printf '%s' "$url" | sed -E 's#github\.com([:/])aweis89/pcode#github.com\1cruxwell/pcode#')
if [ "$new_url" != "$url" ]; then
	git remote set-url origin "$new_url"
	echo "origin: moved to $new_url (was $url)"
fi

git config core.hooksPath .githooks
git fetch --quiet --prune origin
upstream=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/master)
branch=${upstream#origin/}

email=${SYNC_EMAIL:-$(git config --local user.email || git config --global user.email || true)}
if [ -z "$email" ]; then
	echo "no identity to enforce: rerun with SYNC_EMAIL=<address>" >&2
	exit 1
fi
git config --local user.email "$email"
echo "identity: user.email=$email (commits and pushes under any other address are refused)"

# "+ sha" lines: commits on $1 that $upstream lacks by content; "- sha": ones
# it has under another id. Merge commits are never listed. Captured first so a
# failing git cherry stops the script instead of reading as "no commits".
cherry() { git cherry "$upstream" "$1"; }
count() { printf '%s\n' "$1" | grep -c "^$2" || true; }

# The worktree that has branch $1 checked out, if any.
checkout_of() {
	git worktree list --porcelain \
		| awk -v ref="branch refs/heads/$1" '/^worktree /{w=substr($0,10)} $0==ref{print w}'
}

local_head=$(git rev-parse --verify --quiet "refs/heads/$branch" || true)
tree=$(checkout_of "$branch")
if [ -z "$local_head" ]; then
	echo "$branch: no local branch; nothing to update"
elif [ "$local_head" = "$(git rev-parse "$upstream")" ]; then
	echo "$branch: up to date"
elif git merge-base --is-ancestor "$local_head" "$upstream"; then
	if [ -n "$tree" ]; then
		git -C "$tree" merge --quiet --ff-only "$upstream"
	else
		git update-ref "refs/heads/$branch" "$upstream" "$local_head"
	fi
	echo "$branch: fast-forwarded"
else
	lines=$(cherry "$local_head")
	# git cherry skips merges, so match those by tree and author date, which a
	# rewrite keeps: a merge carrying a local-only resolution matches nothing.
	known=$(mktemp)
	git log --merges --format='%T %at' "$upstream" >"$known"
	merges=$(git log --merges --format='%T %at' "$upstream..$local_head" | grep -vxF -f "$known" || true)
	rm -f "$known"
	if [ "$(count "$lines" +)" -eq 0 ] && [ -z "$merges" ]; then
		# Rewritten upstream: everything here exists there under new ids.
		if [ -n "$tree" ]; then
			git -C "$tree" reset --quiet --keep "$upstream"
		else
			git update-ref "refs/heads/$branch" "$upstream" "$local_head"
		fi
		echo "$branch: reset to $upstream (history was rewritten upstream; was $(git rev-parse --short "$local_head"))"
	else
		echo "$branch: has commits or merges $upstream lacks; left alone. Inspect with: git log $upstream..$branch" >&2
	fi
fi

git for-each-ref --format='%(refname:short)' refs/heads | while read -r b; do
	[ "$b" = "$branch" ] && continue
	lines=$(cherry "$b")
	[ "$(count "$lines" -)" -gt 0 ] || continue
	where=$(checkout_of "$b")
	mine=$(count "$lines" +)
	if [ "$mine" -eq 0 ]; then
		echo "$b: on old history, but $upstream already has all of it; safe to delete${where:+ (worktree $where)}" >&2
	else
		echo "$b: $mine commit(s) of its own on old history; run: git ${where:+-C $where }rebase $upstream" >&2
	fi
done
