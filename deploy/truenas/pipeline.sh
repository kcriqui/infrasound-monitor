#!/usr/bin/env bash
# One cycle: pull the Pi's archive and, unless pull-only, rebuild the dashboard.
#
# Runs inside the analysis container (see docker-compose.yml). Everything it
# touches lives under $INFRA_DATA, which is a TrueNAS dataset -- so the pulled
# archive is simultaneously the off-Pi backup, on ZFS with snapshots.
#
# NOT `set -e`: each stage decides for itself whether the next one may run, and
# a failure must never replace a good site with a half-built one.
set -uo pipefail

DATA="${INFRA_DATA:-/data}"
PI="${INFRA_PI:?set INFRA_PI, e.g. infra@100.72.190.123}"
PI_ARCHIVE="${INFRA_PI_ARCHIVE:-infrasound-monitor/archive/}"
PI_KEY="${INFRA_PI_KEY:-$DATA/.ssh/id_infra}"
KNOWN_HOSTS="${INFRA_KNOWN_HOSTS:-$DATA/.ssh/known_hosts}"
CACHE="$DATA/analysis/grid_full.npz"
SITE="$DATA/site"
LOG="$DATA/logs/pipeline.log"

mkdir -p "$DATA/logs" "$DATA/archive"
exec >> "$LOG" 2>&1
ts() { date -Is; }

# The dataset is an SMB share with NFSv4 ACLs and aclmode=restricted, where
# chmod is refused even for root -- so the mounted key cannot be given mode 600
# on the host, and ssh flatly refuses a group-readable private key. Stage a copy
# inside the container instead, where the overlay filesystem has ordinary POSIX
# semantics.
STAGE=/tmp/ssh
install -d -m 700 "$STAGE"
if [ -f "$PI_KEY" ] && install -m 600 "$PI_KEY" "$STAGE/id_infra"; then
    PI_KEY="$STAGE/id_infra"
else
    echo "$(ts) [pull] cannot read $PI_KEY -- check it is readable by uid $(id -u)"
    exit 1
fi

# ---- 1. pull ---------------------------------------------------------------
# No --delete, deliberately: this copy is also the backup, and the archive is
# append-only. If the Pi ever loses its archive, --delete would propagate that
# loss here -- which is the one thing this copy exists to prevent.
# -rlt rather than -a: -a implies -pog, i.e. reproduce the source's permissions
# and ownership. The Pi's uid/gid are meaningless here, the dataset has
# aclmode=restricted so chmod is refused, and we don't run as root so chown
# would fail regardless. Let the dataset's own ACL inheritance decide.
# Strict, not accept-new: the Pi's host key is pinned in .ssh/known_hosts.
# A changed host key should stop the pull, not be silently accepted.
echo "$(ts) [pull] $PI:$PI_ARCHIVE -> $DATA/archive"
RS_LOG="$(mktemp)"
rsync -rlt --omit-dir-times --no-perms --no-owner --no-group --partial --mkpath \
      -e "ssh -i $PI_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS -o ConnectTimeout=30" \
      "$PI:$PI_ARCHIVE" "$DATA/archive/" > "$RS_LOG" 2>&1
rc=$?
cat "$RS_LOG"

# rc 23 = "some files/attrs were not transferred". On this dataset that is the
# expected outcome even on a perfect run: aclmode=restricted makes ZFS refuse
# chmod even from the file's owner, because it would rewrite a non-trivial ACL,
# and rsync always sets a mode on files it creates. The bytes land correctly and
# the ACL-inherited mode is the one we want. So forgive rc 23 -- but only when
# EVERY per-file error was a refused chmod, so a genuine partial transfer still
# fails loudly.
if [ "$rc" -eq 23 ] && grep -q '^rsync:' "$RS_LOG" \
   && ! grep '^rsync:' "$RS_LOG" | grep -qv 'failed to set permissions'; then
    echo "$(ts) [pull] rc=23, but every error was a chmod refused by the dataset ACL -- data is complete"
    rc=0
fi
rm -f "$RS_LOG"
if [ "$rc" -ne 0 ]; then
    echo "$(ts) [pull] FAILED rc=$rc -- skipping rebuild so the site keeps its last good data"
    exit 1
fi

# Pull-only: the NAS is just the snapshotted off-Pi backup, and the Pi rebuilds
# and serves the dashboard for itself. Building here as well would only be a
# second site going stale alongside the real one.
if [ "${INFRA_PULL_ONLY:-0}" = "1" ]; then
    echo "$(ts) [done] pull-only -- no rebuild"
    exit 0
fi

# ---- 2. rebuild ------------------------------------------------------------
# Served by the `web` service (compose profile "serve").
mkdir -p "$DATA/analysis" "$SITE"
echo "$(ts) [build] refresh.py"
python tools/refresh.py "$DATA/archive" --cache "$CACHE" --out-dir "$SITE"
rc=$?
if [ "$rc" -ne 0 ]; then
    echo "$(ts) [build] FAILED rc=$rc$([ "$rc" = 137 ] && echo ' = OOM-killed')"
    exit 1
fi
echo "$(ts) [done]"
