#!/bin/sh
# Show whether this terminal, and every running Pcode.app build, is on the
# latest build, and whether builds/current is behind the source.
set -eu

cd "$(dirname "$0")/../.."

current=$(readlink builds/current 2>/dev/null || echo "")
head=$(git rev-parse --short HEAD)
lock=$(cut -c1-9 app/ghostty.lock)

echo "pcode HEAD:      $head$( [ -n "$(git status --porcelain -- app)" ] && echo ' (app/ dirty)')"
echo "fork lock:       $lock"
echo "builds/current:  ${current:-<none>}"

if [ -n "$current" ]; then
    bin="builds/$current/Contents/MacOS/ghostty"
    case "$current" in
        *"-$head-g"*) ;;
        *) echo "  maybe stale: built from a pcode commit other than HEAD" ;;
    esac
    case "$current" in
        *"-g$lock"* | *"-g$(echo "$lock" | cut -c1-7)"*) ;;
        *) echo "  maybe stale: built from a fork commit other than app/ghostty.lock" ;;
    esac
    newer=$(find app -newer "$bin" -type f \( -name '*.swift' -o -name ghostty.lock -o -name AppIcon.icns \) | head -n 1)
    [ -n "$newer" ] && echo "  STALE: $newer changed after this build (run 'make app')"
fi

this=""
case "${GHOSTTY_RESOURCES_DIR:-}" in
    */builds/*.app/*) this=$(echo "$GHOSTTY_RESOURCES_DIR" | sed 's|.*/builds/\([^/]*\.app\)/.*|\1|') ;;
esac
if [ -n "$this" ]; then
    mark=$( [ "$this" = "$current" ] && echo "latest" || echo "OLD" )
    echo "this terminal:   $this ($mark)"
else
    echo "this terminal:   not a builds/ bundle (TERM_PROGRAM=${TERM_PROGRAM:-?})"
fi

echo "running:"
pgrep -alf '/builds/.*\.app/Contents/MacOS/ghostty' | while read -r pid cmd; do
    app=$(echo "$cmd" | sed 's|.*/builds/\([^/]*\.app\)/.*|\1|')
    started=$(ps -o lstart= -p "$pid")
    mark=$( [ "$app" = "$current" ] && echo "latest" || echo "OLD" )
    echo "  $pid  $started  $app ($mark)"
done
