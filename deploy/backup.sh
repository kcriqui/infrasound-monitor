#!/usr/bin/env bash
# Back up the INFRA20 archive off the Pi's SD card. The archive is the irreplaceable
# "before" baseline and SD cards fail, so keep a second copy somewhere else.
#
#   bash deploy/backup.sh /mnt/usb/infra-backup           # to a mounted disk / local path
#   bash deploy/backup.sh user@nas:/volume1/infra-backup  # to an rsync-over-ssh target
#
# The destination may also come from $INFRA_BACKUP_DEST (how the systemd timer passes it).
# Uses rsync without --delete, so it only ever adds/updates files -- it never removes
# your backup copies even if the source is lost.
#
# A LOCAL destination must live on a filesystem other than the root one, or this aborts:
# an unmounted backup drive turns /mnt/usb/infra-backup into a plain directory on the SD
# card, and rsync would "succeed" at copying the archive onto the very card it is meant to
# protect.  Set INFRA_BACKUP_ALLOW_ROOTFS=1 to stage a copy on the root fs deliberately.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$DEPLOY_DIR")"

DEST="${1:-${INFRA_BACKUP_DEST:-}}"
[ -n "$DEST" ] || { echo "usage: backup.sh <dest>   (or set INFRA_BACKUP_DEST)"; exit 2; }

# Remote target?  rsync's own rule: a colon appearing before any slash means remote.
case "$DEST" in
    *://*) DEST_IS_REMOTE=1 ;;                                                  # rsync://host/path
    *:*)   [ "${DEST%%:*}" = "${DEST%%[:/]*}" ] && DEST_IS_REMOTE=1 || DEST_IS_REMOTE=0 ;;
    *)     DEST_IS_REMOTE=0 ;;
esac

# For a local target, refuse to run if it lands on the root filesystem (see header).
if [ "$DEST_IS_REMOTE" = 0 ] && [ "${INFRA_BACKUP_ALLOW_ROOTFS:-0}" != 1 ]; then
    # rsync --mkpath creates the leaf, so probe the nearest ancestor that exists.
    probe="$DEST"
    while [ ! -e "$probe" ] && [ "$probe" != "/" ]; do probe="$(dirname "$probe")"; done
    if [ "$(stat -c %m -- "$probe")" = "/" ]; then
        echo "backup: $DEST is on the root filesystem -- its backup drive is not mounted." >&2
        echo "        Refusing to copy the archive onto the same disk it already lives on." >&2
        echo "        Mount it (check /etc/fstab, then 'sudo systemctl daemon-reload && sudo mount -a')," >&2
        echo "        or set INFRA_BACKUP_ALLOW_ROOTFS=1 to override." >&2
        exit 1
    fi
fi

# Resolve the archive dir from config.toml (fall back to ./archive).
ARCHIVE="$("$ROOT/.venv/bin/python" - <<'PY' 2>/dev/null || echo "$ROOT/archive"
from infrasound_monitor.config import ARCHIVE_DIR
print(ARCHIVE_DIR)
PY
)"

echo "backup: $ARCHIVE  ->  $DEST"
rsync -a --partial --mkpath "$ARCHIVE/" "$DEST/"
echo "done."
