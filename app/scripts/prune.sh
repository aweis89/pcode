#!/bin/sh
# Move every installed pcode app bundle under builds/ to the Trash except the
# one builds/current points at, and any bundle a running terminal was started
# from (quit it first to prune that one too). Each pruned bundle is also
# unregistered from LaunchServices, so the bundle ID they share resolves to
# the current build.
#
# Usage:
#   app/scripts/prune.sh            # prune
#   app/scripts/prune.sh --dry-run  # only say what would go
set -eu

cd "$(dirname "$0")/../../builds" 2>/dev/null || { echo "no builds/ yet"; exit 0; }

dry=false
[ "${1:-}" = "--dry-run" ] && dry=true

lsregister=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
keep=$(readlink current 2>/dev/null || true)
pruned=0

for app in pcode-*.app; do
    [ -d "$app" ] || continue
    if [ "$app" = "$keep" ]; then
        echo "keep   $app (current)"
        continue
    fi
    if pgrep -f "$PWD/$app/Contents/MacOS/" >/dev/null 2>&1; then
        echo "keep   $app (running; quit it to prune)"
        continue
    fi
    echo "prune  $app"
    pruned=$((pruned + 1))
    $dry && continue
    "$lsregister" -u "$PWD/$app" >/dev/null 2>&1 || true
    mv "$app" "$HOME/.Trash/$app.$(date +%s)"
done

$dry && echo "(dry run: $pruned to prune)" || echo "pruned $pruned"
