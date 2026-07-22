#!/usr/bin/env bash
# Hourly backup: Postgres dump + config (.env, sessions/) -> local staging
# -> Google Drive via rclone. Retention 7 days on both sides.
#
# Cron (VPS):
#   0 * * * * cd /opt/nowaybot && ./scripts/backup.sh >> backups/backup.log 2>&1
#
# Without rclone configured it still keeps local backups and warns -
# a missing Drive setup must never mean "no backup at all".
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE_DIR="${BACKUP_DIR:-$REPO_DIR/backups}"
REMOTE="${RCLONE_REMOTE:-gdrive:nowaybot-backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
PG_SERVICE="${PG_SERVICE:-postgres}"
PG_USER="${PG_USER:-nowaybot}"
PG_DB="${PG_DB:-nowaybot}"
STAMP="$(date -u +%Y%m%d_%H%M%S)"

mkdir -p "$STAGE_DIR"
cd "$REPO_DIR"

# 1. Postgres dump (custom format: compressed, pg_restore-able)
docker compose exec -T "$PG_SERVICE" pg_dump -U "$PG_USER" -d "$PG_DB" \
    --format=custom > "$STAGE_DIR/db_${STAMP}.dump"
echo "dumped db_${STAMP}.dump ($(du -h "$STAGE_DIR/db_${STAMP}.dump" | cut -f1))"

# 2. Config bundle - the identity part: same BOT_TOKEN keeps every indexed
#    telegram_file_id valid, so this is what makes a VPS switch painless.
CONFIG_ITEMS=()
[ -f .env ] && CONFIG_ITEMS+=(".env")
[ -d sessions ] && CONFIG_ITEMS+=("sessions")
if [ "${#CONFIG_ITEMS[@]}" -gt 0 ]; then
    tar czf "$STAGE_DIR/config_${STAMP}.tar.gz" "${CONFIG_ITEMS[@]}"
    echo "bundled config_${STAMP}.tar.gz (${CONFIG_ITEMS[*]})"
fi

# 3. Local retention
find "$STAGE_DIR" -name 'db_*.dump' -mtime +"$RETENTION_DAYS" -delete
find "$STAGE_DIR" -name 'config_*.tar.gz' -mtime +"$RETENTION_DAYS" -delete

# 4. Upload + remote retention
if command -v rclone >/dev/null 2>&1 \
    && rclone listremotes 2>/dev/null | grep -qx "${REMOTE%%:*}:"; then
    rclone copy "$STAGE_DIR/db_${STAMP}.dump" "$REMOTE/"
    [ -f "$STAGE_DIR/config_${STAMP}.tar.gz" ] \
        && rclone copy "$STAGE_DIR/config_${STAMP}.tar.gz" "$REMOTE/"
    rclone delete "$REMOTE" --min-age "${RETENTION_DAYS}d"
    echo "uploaded to $REMOTE, pruned >${RETENTION_DAYS}d"
else
    echo "WARN: rclone remote '${REMOTE%%:*}:' not configured - backup is LOCAL ONLY (see docs/backup.md)"
fi

echo "backup ${STAMP} done"
