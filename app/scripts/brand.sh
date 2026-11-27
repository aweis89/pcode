#!/bin/sh
# Turn a built Ghostty.app (copied to <app>) into Pcode.app in place: its own
# name, bundle ID and icon, so it installs beside stock Ghostty, then ad-hoc
# sign it. Shared by build.sh (local builds) and the app release workflow.
#
# Usage: app/scripts/brand.sh <app> [version]
#
# Env: APP_NAME (default Pcode), BUNDLE_ID (default com.pcode.terminal).
set -eu

app=$1
version=${2:-}
name="${APP_NAME:-Pcode}"
bundle_id="${BUNDLE_ID:-com.pcode.terminal}"
here=$(cd "$(dirname "$0")/.." && pwd)
plist="$app/Contents/Info.plist"
pb() { /usr/libexec/PlistBuddy -c "$1" "$plist"; }

pb "Set :CFBundleIdentifier $bundle_id"
pb "Set :CFBundleName $name"
pb "Set :CFBundleDisplayName $name" 2>/dev/null || pb "Add :CFBundleDisplayName string $name"
if [ -n "$version" ]; then
    pb "Set :CFBundleShortVersionString $version"
    pb "Set :CFBundleVersion $version"
fi
# Sparkle's feeds are upstream Ghostty's; keep automatic checks off for good
# (the fork's AppDelegate.checkForUpdates points at Homebrew instead).
pb "Set :SUEnableAutomaticChecks false" 2>/dev/null || pb "Add :SUEnableAutomaticChecks bool false"

# Upstream's "New Ghostty Tab/Window Here" Finder services open stock Ghostty windows.
pb "Delete :NSServices" 2>/dev/null || true

# Icon. CFBundleIconName selects Ghostty's Icon Composer asset in Assets.car,
# which beats any .icns, so drop it. The dock tile plugin repaints Ghostty's
# icon on pre-Tahoe macOS (and only serves the `macos-icon` option), so drop
# that too.
icns="$here/icon/AppIcon.icns"
if [ -f "$icns" ]; then
    cp "$icns" "$app/Contents/Resources/Pcode.icns"
    pb "Delete :CFBundleIconName" 2>/dev/null || true
    pb "Set :CFBundleIconFile Pcode"
    rm -rf "$app/Contents/PlugIns/DockTilePlugin.plugin"
    rmdir "$app/Contents/PlugIns" 2>/dev/null || true
    pb "Delete :NSDockTilePlugIn" 2>/dev/null || true
else
    echo "warning: $icns missing; keeping Ghostty's icon" >&2
fi

# Bundled fonts (e.g. JetBrains Mono) for app-owned SwiftUI typography.
fonts_dir="$here/fonts"
if [ -d "$fonts_dir" ]; then
    has_fonts=0
    for f in "$fonts_dir"/*.ttf "$fonts_dir"/*.otf; do
        if [ -f "$f" ]; then
            has_fonts=1
            break
        fi
    done
    if [ "$has_fonts" = 1 ]; then
        rm -rf "$app/Contents/Resources/Fonts"
        mkdir -p "$app/Contents/Resources/Fonts"
        for f in "$fonts_dir"/*.ttf "$fonts_dir"/*.otf; do
            [ -f "$f" ] && cp "$f" "$app/Contents/Resources/Fonts/"
        done
        [ -f "$fonts_dir/OFL.txt" ] && cp "$fonts_dir/OFL.txt" "$app/Contents/Resources/Fonts/"
        pb "Set :ATSApplicationFontsPath Fonts" 2>/dev/null || pb "Add :ATSApplicationFontsPath string Fonts"
    fi
fi

# Bundled resources (e.g. prism-bundle.js for syntax highlighting).
resources_dir="$here/resources"
if [ -d "$resources_dir" ]; then
    for f in "$resources_dir"/*; do
        [ -f "$f" ] && cp "$f" "$app/Contents/Resources/"
    done
fi

# Plain ad-hoc: this drops the hardened runtime and entitlements, which only
# matter for notarization. Adding `-o runtime` would need --entitlements
# macos/GhosttyReleaseLocal.entitlements too, or TCC prompts break.
codesign --force --deep --sign - "$app" >/dev/null 2>&1
codesign --verify --deep --strict "$app"
