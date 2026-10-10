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
rsync -rlt --omit-dir-times --no-perms --no-owner --no-group --partial --mkpath \
      -e "ssh -i $PI_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS -o ConnectTimeout=30" \
      "$PI:$PI_ARCHIVE" "$DATA/archive/"
rc=$?
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
# Only if $SITE is a clone of the Pages repo. Without one this still rebuilds
# locally and nginx serves it; the push is the optional public mirror.
if [ -d "$SITE/.git" ]; then
    export GIT_SSH_COMMAND="ssh -i $SITE_KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$KNOWN_HOSTS -o StrictHostKeyChecking=accept-new"
    export GIT_AUTHOR_NAME="infra-nas"    GIT_AUTHOR_EMAIL="infra-nas@localhost"
    export GIT_COMMITTER_NAME="infra-nas" GIT_COMMITTER_EMAIL="infra-nas@localhost"
    cd "$SITE" || exit 1
    git add -A
    if git rev-parse --verify HEAD >/dev/null 2>&1; then
        git commit --amend -m "site update $(ts)" --quiet 2>/dev/null
    else
        git commit -m "initial site" --quiet 2>/dev/null
    fi
    if git remote | grep -q .; then
        if git push --force --quiet; then
            echo "$(ts) [publish] pushed"
        else
            echo "$(ts) [publish] push FAILED -- site is still correct locally"
        fi
    fi
else
    echo "$(ts) [publish] $SITE is not a git clone -- serving locally only"
fi
echo "$(ts) [done]"
