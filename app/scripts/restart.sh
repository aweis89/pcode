#!/bin/sh
# Quit every running pcode Ghostty and open builds/current, with the same
# windows, tabs and splits. Each terminal that was showing a pcode session
# reopens it (`pcode -c <id>`, from the `pcode;session` APC pcode sends).
#
# Hosted sessions keep running across the restart; a session running inside
# its terminal ends with it and is resumed from its saved transcript, so a
# turn in flight there is lost.
#
# Every instance shares the bundle ID, hence one saved state: only the last
# to quit is restored. That is the one this script runs in, if any.
#
# Usage: app/scripts/restart.sh (make app-restart)
set -eu

cd "$(dirname "$0")/../.."
root=$(pwd)
self="$root/app/scripts/restart.sh"
bundle_id="${BUNDLE_ID:-com.pcode.terminal}"
log="${TMPDIR:-/tmp}/pcode-terminal-restart.log"

current=$(readlink builds/current 2>/dev/null || true)
if [ -z "$current" ] || [ ! -d "builds/$current" ]; then
    echo "error: builds/current not found; run 'make app'" >&2
    exit 1
fi

# Run from one of the terminals being quit, this script would die with it:
# carry on detached, and report to the log.
if [ "${PCODE_RESTART_DETACHED:-}" != 1 ]; then
    app/scripts/status.sh | grep -E 'STALE|maybe stale' || true
    # Before detaching: the terminal we run in is still our ancestor.
    owner=""
    pid=$$
    while [ "$pid" -gt 1 ]; do
        case "$(ps -o command= -p "$pid")" in
            "$root"/builds/*/Contents/MacOS/ghostty*) owner=$pid; break ;;
        esac
        pid=$(ps -o ppid= -p "$pid" | tr -d ' ')
    done
    echo "restarting into builds/$current; log: $log"
    PCODE_RESTART_DETACHED=1 PCODE_RESTART_OWNER="$owner" nohup "$self" >"$log" 2>&1 </dev/null &
    exit 0
fi

owner="${PCODE_RESTART_OWNER:-}"
# -a: macOS pgrep otherwise skips our own ancestors.
pids=$(pgrep -a -f "$root/builds/[^/]*/Contents/MacOS/ghostty" || true)
# The owner last, so its windows are the ones restored.
order=""
for pid in $pids; do
    [ "$pid" = "$owner" ] || order="$order $pid"
done
[ -n "$owner" ] && order="$order $owner"
[ "$(echo $order | wc -w)" -gt 1 ] && echo "note: several instances; only the last quit ($owner) is restored"

if grep -qsE '^\s*window-save-state\s*=\s*never' "$HOME/.config/ghostty/config" \
    "$HOME/Library/Application Support/$bundle_id/config"; then
    echo "warning: window-save-state = never; the new app will not restore windows"
fi

# Restoration only happens if macOS kept the windows at quit, which by default
# it does not ("Close windows when quitting an app"). Force it for this quit;
# the new app resets the key from `window-save-state` when it loads its config.
defaults write "$bundle_id" NSQuitAlwaysKeepsWindows -bool true

# The pcode clients in an app's terminals: not their session hosts, which
# descend from them too but outlive the terminal by design.
pcode_clients() {
    for p in $(descendants "$1"); do
        ps -o command= -p "$p" | grep -qE '/bin/pcode( |$)' && echo "$p"
    done
    true
}

descendants() {
    ps -A -o pid=,ppid= | awk -v root="$1" '
        { parent[$1] = $2 }
        END {
            for (p in parent) {
                q = parent[p]
                while (q > 1) { if (q == root) { print p; break } q = parent[q] }
            }
        }'
}

for pid in $order; do
    app=$(ps -o command= -p "$pid" | sed 's|/Contents/MacOS/ghostty.*||')
    # pcode runs in its own process group and may still hold its session's
    # lock after the app is gone; `pcode -c` would then open a copy.
    clients=$(pcode_clients "$pid")
    echo "quitting $pid ($app); confirm its quit dialog if one appears"
    osascript -e "tell application \"$app\" to quit" || true
    while kill -0 "$pid" 2>/dev/null; do sleep 0.1; done
    for p in $clients; do
        i=0
        while kill -0 "$p" 2>/dev/null && [ "$i" -lt 150 ]; do i=$((i + 1)); sleep 0.1; done
        kill -0 "$p" 2>/dev/null && echo "note: pcode $p still running; its pane may open a copy" || true
    done
done

echo "opening builds/$current"
open "$root/builds/$current"
