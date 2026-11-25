#!/bin/sh
# Build Pcode.app: the Ghostty fork (aweis89/ghostty-smooth) at the commit in
# app/ghostty.lock, with pcode's Swift from app/macos copied into it, then
# install it as builds/current.
#
# The fork owns the terminal core (renderer, glide, the pcode APC action) and
# one Swift seam, PCodeAppHooks; app/macos/PCode implements that seam and
# everything pcode-specific. The fork ignores the two overlay directories, so
# copying into a fork checkout never dirties it.
#
# Usage:
#   app/scripts/build.sh               # build and install (main checkout only)
#   app/scripts/build.sh --out X.app   # build, then copy and brand it as X.app
#   app/scripts/build.sh --no-install  # build only (a compile check)
#
# --out is for test builds and CI: tmp/ghostty is shared, so copy a build out
# with this rather than afterwards, when another session's may have replaced it.
#
# Env:
#   APP_NAME, BUNDLE_ID  the bundle's name and ID (brand.sh): a test build
#                    needs its own ID to stay apart from the real app
#   APP_VERSION      the bundle's version, with --out
#   GHOSTTY_SRC      build in this fork checkout as it is, not tmp/ghostty at
#                    the lock: for trying fork changes before bumping the lock
#   GHOSTTY_VERSION  passed to zig as -Dversion-string
#   ZIG_FLAGS        extra zig build flags
#
# Launch: builds/current/Contents/MacOS/ghostty
set -eu

cd "$(dirname "$0")/../.."
repo=$(pwd)

install=1
out=""
case "${1:-}" in
    --no-install) install=0 ;;
    --out)
        install=0
        out=${2:?usage: build.sh --out <path>.app}
        case "$out" in /*) ;; *) out="$(pwd)/$out" ;; esac
        ;;
    "") ;;
    *) echo "usage: build.sh [--no-install | --out <path>.app]" >&2; exit 2 ;;
esac

# Installing from a worktree would ship that branch's Swift as builds/current,
# which the next install from the main checkout silently replaces.
if [ "$install" = 1 ] && [ "$(git rev-parse --git-dir)" != "$(git rev-parse --git-common-dir)" ]; then
    echo "error: install from the main checkout; in a worktree use --no-install" >&2
    exit 1
fi

lock=$(tr -d '[:space:]' < app/ghostty.lock)
url="${GHOSTTY_URL:-https://github.com/aweis89/ghostty-smooth.git}"

if [ -n "${GHOSTTY_SRC:-}" ]; then
    src=$GHOSTTY_SRC
else
    # tmp/ is shared by every worktree (it is a symlink there), so one clone
    # and one zig cache serve them all; builds take turns.
    src=tmp/ghostty
    mkdir -p tmp
    if ! mkdir tmp/ghostty.building 2>/dev/null; then
        echo "error: another build is using $src (remove tmp/ghostty.building if none is)" >&2
        exit 1
    fi
    trap 'rmdir "$repo/tmp/ghostty.building"' EXIT INT TERM
    if [ ! -d "$src/.git" ]; then
        git clone --quiet --filter=blob:none --no-checkout "$url" "$src"
    fi
    git -C "$src" cat-file -e "$lock^{commit}" 2>/dev/null || git -C "$src" fetch --quiet origin "$lock"
    git -C "$src" checkout --quiet --detach "$lock"
fi

# --delete: a Swift file removed here must not linger in the build.
rsync -a --delete app/macos/PCode/ "$src/macos/Sources/Features/PCode/"
# The fuzzy-match cases pcode's own tests pin, so both ports test one file.
rsync -a --delete app/macos/Tests/ tests/data/pick_fuzzy_cases.json "$src/macos/Tests/Features/PCode/"

# shellcheck disable=SC2086
(cd "$src" && zig build -Doptimize=ReleaseFast \
    ${GHOSTTY_VERSION:+-Dversion-string="$GHOSTTY_VERSION"} ${ZIG_FLAGS:-})

built="$src/macos/build/ReleaseLocal/Ghostty.app"
echo "built $built"

if [ -n "$out" ]; then
    rm -rf "$out"
    mkdir -p "$(dirname "$out")"
    cp -R "$built" "$out"
    app/scripts/brand.sh "$out" "${APP_VERSION:-}"
    echo "wrote $out"
    exit 0
fi
[ "$install" = 1 ] || exit 0

# Install into a new versioned bundle: overwriting a running one makes macOS
# kill it when its code signature pages stop matching.
name="${APP_NAME:-pcode}"
bundle_id="${BUNDLE_ID:-com.pcode.terminal}"
rev="$(git rev-parse --short HEAD)-g$(git -C "$src" rev-parse --short HEAD)"
if [ -n "$(git status --porcelain -- app)" ] || [ -n "${GHOSTTY_SRC:-}" ]; then
    rev="$rev-dirty-$(date +%H%M%S)"
fi
dest="builds/${name}-${rev}.app"
tmp="${dest}.tmp"

trash() {
    if [ -e "$1" ]; then
        mv "$1" "$HOME/.Trash/$(basename "$1").$(date +%s)"
    fi
}

mkdir -p builds
trash "$tmp"
cp -R "$built" "$tmp"
APP_NAME="$name" BUNDLE_ID="$bundle_id" app/scripts/brand.sh "$tmp"
trash "$dest"
mv "$tmp" "$dest"
ln -sfn "$(basename "$dest")" builds/current

/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$dest" || true

echo "installed $dest (builds/current -> $(basename "$dest"))"
