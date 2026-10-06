#!/bin/sh
# Install (or reinstall) every launchd job in scripts/launchd/ for this checkout:
# the daily trades backfill and the order-book recorder.
#   scripts/install_daily_jobs.sh             install and load
#   scripts/install_daily_jobs.sh --uninstall unload and remove
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
agents="$HOME/Library/LaunchAgents"
domain="gui/$(id -u)"

for template in "$repo"/scripts/launchd/*.plist.template; do
    label=$(basename "$template" .plist.template)
    dest="$agents/$label.plist"
    launchctl bootout "$domain/$label" 2>/dev/null || true
    if [ "${1:-}" = "--uninstall" ]; then
        rm -f "$dest"
        echo "removed $label"
        continue
    fi
    uv=$(command -v uv) || { echo "uv not found on PATH" >&2; exit 1; }
    mkdir -p "$agents" "$repo/logs"
    sed -e "s|__REPO__|$repo|g" -e "s|__UV__|$uv|g" "$template" > "$dest"
    plutil -lint -s "$dest"
    launchctl bootstrap "$domain" "$dest"
    echo "installed $label -> $dest"
done
