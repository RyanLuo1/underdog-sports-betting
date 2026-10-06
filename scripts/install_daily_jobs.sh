#!/bin/sh
# Install (or reinstall) every launchd job in scripts/launchd/ for this checkout:
# the daily trades backfill, the order-book recorder, and the nightly stream backup.
#   scripts/install_daily_jobs.sh             install and load; a loaded job whose
#                                             plist is unchanged is left running
#   scripts/install_daily_jobs.sh --uninstall unload and remove
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
agents="$HOME/Library/LaunchAgents"
domain="gui/$(id -u)"

for template in "$repo"/scripts/launchd/*.plist.template; do
    label=$(basename "$template" .plist.template)
    dest="$agents/$label.plist"
    if [ "${1:-}" = "--uninstall" ]; then
        launchctl bootout "$domain/$label" 2>/dev/null || true
        rm -f "$dest"
        echo "removed $label"
        continue
    fi
    uv=$(command -v uv) || { echo "uv not found on PATH" >&2; exit 1; }
    mkdir -p "$agents" "$repo/logs"
    rendered=$(mktemp)
    sed -e "s|__REPO__|$repo|g" -e "s|__UV__|$uv|g" "$template" > "$rendered"
    chmod 644 "$rendered"
    plutil -lint -s "$rendered"
    if cmp -s "$rendered" "$dest" && launchctl print "$domain/$label" >/dev/null 2>&1; then
        rm -f "$rendered"
        echo "unchanged $label"
        continue
    fi
    launchctl bootout "$domain/$label" 2>/dev/null || true
    mv "$rendered" "$dest"
    launchctl bootstrap "$domain" "$dest"
    echo "installed $label -> $dest"
done
