#!/usr/bin/env bash
# Schedule pipeline.sh once a day. A sleep-until-target loop rather than cron so
# the container is self-contained and does not depend on TrueNAS's scheduler --
# recomputing the target each pass means it neither drifts nor doubles up across
# a DST change.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEDULE="${INFRA_SCHEDULE:-06:15}"

echo "infra analysis loop: daily at $SCHEDULE $(date +%Z)"
[ "${INFRA_RUN_AT_START:-0}" = "1" ] && bash "$HERE/pipeline.sh"

while :; do
    now=$(date +%s)
    target=$(date -d "today $SCHEDULE" +%s)
    [ "$target" -le "$now" ] && target=$(date -d "tomorrow $SCHEDULE" +%s)
    echo "$(date -Is) sleeping $((target - now))s until $(date -d "@$target" -Is)"
    sleep $((target - now))
    bash "$HERE/pipeline.sh" || echo "$(date -Is) pipeline exited non-zero (see logs/pipeline.log)"
done
