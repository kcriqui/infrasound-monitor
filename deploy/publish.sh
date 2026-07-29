#!/usr/bin/env bash
# Daily dashboard publish (Linux / Raspberry Pi). Run by infra-dashboard.service (the
# timer fires it). Extends the PSD grid to now, rebuilds the dashboard + interactive
# waterfall into <project>/site, and -- if <project>/site is a git clone of a static-host
# repo (e.g. a GitHub Pages repo) -- commits and force-pushes a SINGLE amended commit, so
# the published repo never accumulates history/bloat. Mirrors deploy/publish.ps1.
#
# One-time setup of the push target (a deploy key + cloning the Pages repo into site/) is
# separate -- see the "Publishing the dashboard" section of DEPLOY.md. Without it, this
# still rebuilds the site locally and just skips the push.
#
# NOT `set -e`: a push failure must never crash the timer or discard a good rebuild.
set -uo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$DEPLOY_DIR")"
cd "$ROOT"
VENV_PY="$ROOT/.venv/bin/python"
SITE_DIR="${1:-$ROOT/site}"
CACHE="$ROOT/analysis/grid_full.npz"
LOG="$DEPLOY_DIR/publish.log"
ARCHIVE="$("$VENV_PY" -c 'from infrasound_monitor.config import ARCHIVE_DIR; print(ARCHIVE_DIR)' 2>/dev/null || echo "$ROOT/archive")"

# identity for the commit (so a headless box never trips "please tell me who you are")
export GIT_AUTHOR_NAME="infra-pi"    GIT_AUTHOR_EMAIL="infra-pi@localhost"
export GIT_COMMITTER_NAME="infra-pi" GIT_COMMITTER_EMAIL="infra-pi@localhost"

ts() { date -Is; }
echo "$(ts)  [publish] rebuilding dashboard ..." >> "$LOG"
"$VENV_PY" tools/refresh.py "$ARCHIVE" --cache "$CACHE" --out-dir "$SITE_DIR" >> "$LOG" 2>&1

if [ -d "$SITE_DIR/.git" ]; then
    cd "$SITE_DIR"
    git add -A
    if git rev-parse --verify HEAD >/dev/null 2>&1; then
        git commit --amend -m "site update $(ts)" --quiet 2>/dev/null    # keep history at one commit
    else
        git commit -m "initial site" --quiet 2>/dev/null
    fi
    if git remote | grep -q .; then
        if git push --force --quiet 2>>"$LOG"; then
            echo "$(ts)  [publish] pushed" >> "$LOG"
        else
            echo "$(ts)  [publish] push FAILED (see log above)" >> "$LOG"
        fi
    else
        echo "$(ts)  [publish] rebuilt; no git remote (skipped push)" >> "$LOG"
    fi
else
    echo "$(ts)  [publish] rebuilt -> $SITE_DIR (git not set up; local only)" >> "$LOG"
fi
