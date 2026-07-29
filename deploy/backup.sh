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
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$DEPLOY_DIR")"

DEST="${1:-${INFRA_BACKUP_DEST:-}}"
[ -n "$DEST" ] || { echo "usage: backup.sh <dest>   (or set INFRA_BACKUP_DEST)"; exit 2; }

# Resolve the archive dir from config.toml (fall back to ./archive).
ARCHIVE="$("$ROOT/.venv/bin/python" - <<'PY' 2>/dev/null || echo "$ROOT/archive"
from infrasound_monitor.config import ARCHIVE_DIR
print(ARCHIVE_DIR)
PY
)"

echo "backup: $ARCHIVE  ->  $DEST"
rsync -a --partial --mkpath "$ARCHIVE/" "$DEST/"
echo "done."
