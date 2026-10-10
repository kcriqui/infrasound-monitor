#!/usr/bin/env bash
# One cycle: pull the Pi's archive, rebuild the dashboard, publish it.
#
# Runs inside the analysis container (see docker-compose.yml). Everything it
# touches lives under $INFRA_DATA, which is a TrueNAS dataset -- so the pulled
# archive is simultaneously the off-Pi backup, on ZFS with snapshots.
#
# NOT `set -e`: each stage decides for itself whether the next one may run, and
# a failure must never leave a half-built site published.
set -uo pipefail

DATA="${INFRA_DATA:-/data}"
PI="${INFRA_PI:?set INFRA_PI, e.g. infra@100.72.190.123}"
PI_ARCHIVE="${INFRA_PI_ARCHIVE:-infrasound-monitor/archive/}"
PI_KEY="${INFRA_PI_KEY:-$DATA/.ssh/id_infra}"
SITE_KEY="${INFRA_SITE_KEY:-$DATA/.ssh/id_site}"
CACHE="$DATA/analysis/grid_full.npz"
SITE="$DATA/site"
LOG="$DATA/logs/pipeline.log"

mkdir -p "$DATA/logs" "$DATA/archive" "$DATA/analysis" "$SITE"

# The dataset is an SMB share with NFSv4 ACLs and aclmode=restricted, where
# chmod is refused even for root -- so the mounted keys cannot be given mode 600
# on the host, and ssh flatly refuses a group-readable private key. Stage copies
# inside the container instead, where the overlay filesystem has ordinary POSIX
# semantics. Copying as root also makes them root-owned, which ssh requires.
STAGE=/tmp/ssh
install -d -m 700 "$STAGE"
for pair in "$PI_KEY:id_infra" "$SITE_KEY:id_site"; do
    src="${pair%:*}"; dst="$STAGE/${pair##*:}"
    [ -f "$src" ] && install -m 600 "$src" "$dst"
done
if [ -f "$STAGE/id_infra" ]; then
    PI_KEY="$STAGE/id_infra"
else
    echo "$(ts) [pull] cannot read $PI_KEY -- check it is readable by uid $(id -u)"
    exit 1
fi
[ -f "$SITE_KEY" ] && [ -f "$STAGE/id_site"  ] && SITE_KEY="$STAGE/id_site"
KNOWN_HOSTS="${INFRA_KNOWN_HOSTS:-$DATA/.ssh/known_hosts}"
exec >> "$LOG" 2>&1
ts() { date -Is; }

# ---- 1. pull ---------------------------------------------------------------
# No --delete, deliberately: this copy is also the backup, and the archive is
# append-only. If the Pi ever loses its archive, --delete would propagate that
# loss here -- which is the one thing this copy exists to prevent.
# -rlt rather than -a: -a implies -pog, i.e. reproduce the source's permissions
# and ownership. The Pi's uid/gid are meaningless here, the dataset has
# aclmode=restricted so chmod is refused, and we no longer run as root so chown
# would fail regardless. Let the dataset's own ACL inheritance decide.
# Strict, not accept-new: the Pi's host key is pinned in .ssh/known_hosts,
# which is mounted read-only so ssh could not record it on first use anyway.
# A changed host key should stop the pull, not be silently accepted.
echo "$(ts) [pull] $PI:$PI_ARCHIVE -> $DATA/archive"
RS_LOG="$(mktemp)"
rsync -rlt --omit-dir-times --no-perms --no-owner --no-group --partial --mkpath       -e "ssh -i $PI_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS -o ConnectTimeout=30"       "$PI:$PI_ARCHIVE" "$DATA/archive/" > "$RS_LOG" 2>&1
rc=$?
cat "$RS_LOG"

# rc 23 = "some files/attrs were not transferred". On this dataset that is the
# expected outcome even on a perfect run: aclmode=restricted makes ZFS refuse
# chmod even from the file's owner, because it would rewrite a non-trivial ACL,
# and rsync always sets a mode on files it creates. The bytes land correctly and
# the ACL-inherited mode is the one we want. So forgive rc 23 -- but only when
# EVERY per-file error was a refused chmod, so a genuine partial transfer still
# fails loudly.
if [ "$rc" -eq 23 ] && grep -q '^rsync:' "$RS_LOG"    && ! grep '^rsync:' "$RS_LOG" | grep -qv 'failed to set permissions'; then
    echo "$(ts) [pull] rc=23, but every error was a chmod refused by the dataset ACL -- data is complete"
    rc=0
fi
rm -f "$RS_LOG"
if [ "$rc" -ne 0 ]; then
    echo "$(ts) [pull] FAILED rc=$rc -- skipping rebuild so the site keeps its last good data"
    exit 1
fi

# ---- 2. rebuild ------------------------------------------------------------
echo "$(ts) [build] refresh.py"
python tools/refresh.py "$DATA/archive" --cache "$CACHE" --out-dir "$SITE"
rc=$?
if [ "$rc" -ne 0 ]; then
    echo "$(ts) [build] FAILED rc=$rc$([ "$rc" = 137 ] && echo ' = OOM-killed') -- not publishing"
    exit 1
fi

# ---- 3. publish ------------------------------------------------------------
# Optional mirror to GitHub Pages. The clone lives in the container's own
# filesystem, never in the dataset: git chmods its lock files, and this dataset
# (aclmode=restricted) refuses chmod even for the owner, so a clone there fails
# on `git clone` and would keep failing on every add/commit. Keeping it out also
# means no .git ends up in the directory nginx serves.
if [ -n "${INFRA_SITE_REPO:-}" ]; then
    export GIT_SSH_COMMAND="ssh -i $SITE_KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$KNOWN_HOSTS -o StrictHostKeyChecking=accept-new"
    export GIT_AUTHOR_NAME="infra-nas"    GIT_AUTHOR_EMAIL="infra-nas@localhost"
    export GIT_COMMITTER_NAME="infra-nas" GIT_COMMITTER_EMAIL="infra-nas@localhost"
    WORK="$(mktemp -d)"
    if git clone --depth 1 -q "$INFRA_SITE_REPO" "$WORK/site" 2>&1; then
        find "$WORK/site" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
        cp -r "$SITE"/. "$WORK/site"/
        cd "$WORK/site"
        git add -A
        if git diff --cached --quiet; then
            echo "$(ts) [publish] no change since the last push"
        else
            git commit --amend -m "site update $(ts)" --quiet 2>/dev/null                 || git commit -m "site update $(ts)" --quiet
            if git push --force --quiet; then
                echo "$(ts) [publish] pushed to $INFRA_SITE_REPO"
            else
                echo "$(ts) [publish] push FAILED -- the local site is still correct"
            fi
        fi
    else
        echo "$(ts) [publish] could not clone $INFRA_SITE_REPO -- serving locally only"
    fi
    cd /; rm -rf "$WORK"
else
    echo "$(ts) [publish] INFRA_SITE_REPO unset -- serving locally only"
fi
echo "$(ts) [done]"
