#!/bin/sh
# Tag origin's mainline as the next release and push the tag; publish.yml does
# the rest (PyPI, GitHub Release, Homebrew formula).
#
#   make release                  # next patch: v0.1.0 -> v0.1.1
#   make release BUMP=minor       # v0.1.0 -> v0.2.0 (or BUMP=major)
#   make release VERSION=0.2.0rc1 # an exact version; pre-releases skip Homebrew
#
# Tags what origin has, never the local checkout, and refuses a commit whose
# CI run didn't pass (FORCE=1 overrides). YES=1 skips the confirmation.
set -eu

cd "$(git rev-parse --show-toplevel)"
git fetch --quiet --tags origin
upstream=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/master)
commit=$(git rev-parse "$upstream")

latest=$(git tag -l 'v*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1 || true)
if [ -n "${VERSION:-}" ]; then
	next=${VERSION#v}
elif [ -z "$latest" ]; then
	echo "no release tag yet; pass VERSION=x.y.z" >&2
	exit 1
else
	next=$(printf '%s\n' "${latest#v}" | awk -F. -v bump="${BUMP:-patch}" '
		bump == "major" { print $1 + 1 ".0.0"; next }
		bump == "minor" { print $1 "." $2 + 1 ".0"; next }
		bump == "patch" { print $1 "." $2 "." $3 + 1; next }
		{ exit 1 }') || { echo "BUMP must be major, minor or patch" >&2; exit 1; }
fi
tag="v$next"

# PEP 440 as hatch-vcs writes it: anything else builds a differently named
# package and the publish run fails after the tag is already out.
if ! printf '%s\n' "$next" | grep -qE '^[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?$'; then
	echo "VERSION $next is not like 1.2.3 or 1.2.3rc1" >&2
	exit 1
fi
if [ -n "$latest" ] && [ "${FORCE:-}" != 1 ] &&
	[ "$(printf '%s\n%s\n' "${latest#v}" "$next" | sort -V | tail -n 1)" != "$next" ]; then
	echo "$tag is not newer than $latest; FORCE=1 to release it anyway" >&2
	exit 1
fi

if git rev-parse --quiet --verify "refs/tags/$tag" >/dev/null; then
	echo "$tag already exists" >&2
	exit 1
fi
if [ -n "$latest" ] && [ "$(git rev-parse "$latest^{commit}")" = "$commit" ]; then
	echo "$upstream is already released as $latest" >&2
	exit 1
fi

if command -v gh >/dev/null 2>&1; then
	ci=$(gh run list --commit "$commit" --workflow ci --json status,conclusion \
		--jq '.[0] | if . == null then "none" elif .conclusion == "" then .status else .conclusion end' 2>/dev/null || echo unknown)
else
	ci="unknown (no gh)"
fi
if [ "$ci" != success ] && [ "${FORCE:-}" != 1 ]; then
	echo "CI for $(git rev-parse --short "$commit") is '$ci', not success; FORCE=1 to release anyway" >&2
	exit 1
fi

echo "Release $tag at $upstream ($(git rev-parse --short "$commit"), CI: $ci)"
if [ -n "$latest" ]; then
	echo "Changes since $latest:"
	git log --oneline --no-merges "$latest..$commit" | sed 's/^/  /'
fi
if [ "${YES:-}" != 1 ]; then
	printf 'Tag and push %s? [y/N] ' "$tag"
	read -r answer || answer=
	[ "$answer" = y ] || [ "$answer" = Y ] || { echo "aborted"; exit 1; }
fi

git tag -a "$tag" -m "pcode $next" "$commit"
git push --quiet origin "refs/tags/$tag"
echo "Pushed $tag. Follow the release: gh run list --workflow publish --limit 1"
