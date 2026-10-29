#!/usr/bin/env bash
# Daily dashboard rebuild (Linux / Raspberry Pi). Run by infra-dashboard.service (the
# timer fires it). Extends the PSD grid to now and rebuilds the dashboard + interactive
# waterfall into <project>/site, which Tailscale Funnel serves directly
# (`tailscale funnel --bg <project>/site` -- see "Serving the dashboard" in DEPLOY.md).
#
# The pages are built in a scratch directory and moved into place only once both are
# complete: site/ is served live, so it must never hold a half-written page, and a
# failed rebuild leaves yesterday's good pages up.
set -uo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$DEPLOY_DIR")"
cd "$ROOT"
VENV_PY="$ROOT/.venv/bin/python"
SITE_DIR="${1:-$ROOT/site}"
CACHE="$ROOT/analysis/grid_full.npz"
LOG="$DEPLOY_DIR/publish.log"
ARCHIVE="$("$VENV_PY" -c 'from infrasound_monitor.config import ARCHIVE_DIR; print(ARCHIVE_DIR)' 2>/dev/null || echo "$ROOT/archive")"

ts() { date -Is; }
BUILD="$SITE_DIR.building"
rm -rf "$BUILD"
echo "$(ts)  [publish] rebuilding dashboard ..." >> "$LOG"
"$VENV_PY" tools/refresh.py "$ARCHIVE" --cache "$CACHE" --out-dir "$BUILD" >> "$LOG" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    # rc 137 = OOM-killed (SIGKILL); give more swap / RAM.
    echo "$(ts)  [publish] rebuild FAILED (refresh.py exit $rc$([ "$rc" = 137 ] && echo ' = OOM-killed')) -- keeping the last good site" >> "$LOG"
    rm -rf "$BUILD"
    exit 1
fi

mkdir -p "$SITE_DIR"
# The interactive waterfall first, so the dashboard's link to it never dangles.
mv -f "$BUILD/waterfall_full.html" "$SITE_DIR/" && mv -f "$BUILD/index.html" "$SITE_DIR/"
rm -rf "$BUILD"
echo "$(ts)  [publish] rebuilt -> $SITE_DIR" >> "$LOG"
