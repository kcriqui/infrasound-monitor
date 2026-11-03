#!/usr/bin/env bash
# Pull the Pi's archive onto this dataset: the station's snapshotted off-Pi backup.
# Run daily by a TrueNAS cron job, as the dataset's owner (see README.md). Settings
# are in deploy/truenas/.env beside this script, which is git-ignored.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/.env" ] && . "$HERE/.env"

DATA="${INFRA_DATA:-$(dirname "$(dirname "$HERE")")}"     # the repo clone is the dataset root
PI="${INFRA_PI:?set INFRA_PI in $HERE/.env, e.g. infra@raspberrypi}"
PI_ARCHIVE="${INFRA_PI_ARCHIVE:-infrasound-monitor/archive/}"
PI_KEY="$DATA/.ssh/id_infra"
KNOWN_HOSTS="$DATA/.ssh/known_hosts"
LOG="$DATA/logs/pull.log"

mkdir -p "$DATA/logs" "$DATA/archive"
exec >> "$LOG" 2>&1
ts() { date -Is; }

# The dataset is an SMB share with NFSv4 ACLs and aclmode=restricted, where chmod is
# refused even for root -- so the key cannot be given mode 600 where it lives, and
# ssh flatly refuses a group-readable private key. Use a private copy in /tmp.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
if ! install -m 600 "$PI_KEY" "$STAGE/id_infra"; then
    echo "$(ts) [pull] FAILED: cannot read $PI_KEY -- check it is readable by uid $(id -u)"
    exit 1
fi

# No --delete, deliberately: this copy is the backup, and the archive is append-only.
# If the Pi ever loses its archive, --delete would propagate that loss here -- the one
# thing this copy exists to prevent.
# -rlt rather than -a: -a implies -pog, and the Pi's modes and ids mean nothing here.
# Let the dataset's own ACL inheritance decide.
# Strict, not accept-new: the Pi's host key is pinned in .ssh/known_hosts, so a changed
# host key stops the pull rather than being silently accepted.
echo "$(ts) [pull] $PI:$PI_ARCHIVE -> $DATA/archive"
RS_LOG="$STAGE/rsync.log"
rsync -rlt --omit-dir-times --no-perms --no-owner --no-group --partial --mkpath \
      -e "ssh -i $STAGE/id_infra -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS -o ConnectTimeout=30" \
      "$PI:$PI_ARCHIVE" "$DATA/archive/" > "$RS_LOG" 2>&1
rc=$?
cat "$RS_LOG"

# rc 23 = "some files/attrs were not transferred". On this dataset that is the expected
# outcome even on a perfect run: ZFS refuses the chmod rsync always does on files it
# creates, and the bytes land correctly. So forgive rc 23 -- but only when EVERY per-file
# error was a refused chmod, so a genuine partial transfer still fails loudly.
if [ "$rc" -eq 23 ] && grep -q '^rsync:' "$RS_LOG" \
   && ! grep '^rsync:' "$RS_LOG" | grep -qv 'failed to set permissions'; then
    echo "$(ts) [pull] rc=23, but every error was a chmod refused by the dataset ACL -- data is complete"
    rc=0
fi
if [ "$rc" -ne 0 ]; then
    echo "$(ts) [pull] FAILED rc=$rc"
    exit 1
fi
echo "$(ts) [done]"
