#!/usr/bin/env bash
# Hourly backup: Postgres dump + config (.env, sessions/) -> local staging
# -> Google Drive via rclone. Retention 24 HOURS, and Drive-only: the local
# dump is deleted right after a successful upload so nothing piles up on the
# VPS disk.
#
# Cron (VPS):
#   0 * * * * cd /opt/nowaybot && ./scripts/backup.sh >> backups/backup.log 2>&1
#
# Fallback: if rclone is NOT configured the local copy is KEPT (and pruned to
# 24h) instead - a missing Drive setup must never mean "no backup at all".
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE_DIR="${BACKUP_DIR:-$REPO_DIR/backups}"
REMOTE="${RCLONE_REMOTE:-gdrive:nowaybot-backups}"
# Keep only the last day of hourly dumps (~24). Legacy RETENTION_DAYS still
# honoured for anyone overriding it; converted to hours.
RETENTION_HOURS="${RETENTION_HOURS:-${RETENTION_DAYS:+$((RETENTION_DAYS * 24))}}"
RETENTION_HOURS="${RETENTION_HOURS:-24}"
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

# 3. Upload + remote retention (Drive is the system of record)
uploaded=false
if command -v rclone >/dev/null 2>&1 \
    && rclone listremotes 2>/dev/null | grep -qx "${REMOTE%%:*}:"; then
    rclone copy "$STAGE_DIR/db_${STAMP}.dump" "$REMOTE/"
    [ -f "$STAGE_DIR/config_${STAMP}.tar.gz" ] \
        && rclone copy "$STAGE_DIR/config_${STAMP}.tar.gz" "$REMOTE/"
    rclone delete "$REMOTE" --min-age "${RETENTION_HOURS}h"
    echo "uploaded to $REMOTE, pruned >${RETENTION_HOURS}h"
    uploaded=true
else
    echo "WARN: rclone remote '${REMOTE%%:*}:' not configured - backup is LOCAL ONLY (see docs/backup.md)"
fi

# 4. Local cleanup. Preference is Drive-ONLY: once a dump is safely on Drive
#    the local staging copy is removed so nothing accumulates on the VPS. If
#    the upload did NOT happen, the local copy is instead KEPT and pruned to
#    the 24h window - never zero backups.
if [ "$uploaded" = true ]; then
    rm -f "$STAGE_DIR/db_${STAMP}.dump" "$STAGE_DIR/config_${STAMP}.tar.gz"
    echo "removed local staging (Drive-only)"
    # Sweep any strays left by earlier local-only runs so old backups can't
    # linger on disk after Drive is configured.
    find "$STAGE_DIR" -name 'db_*.dump' -delete
    find "$STAGE_DIR" -name 'config_*.tar.gz' -delete
else
    find "$STAGE_DIR" -name 'db_*.dump' -mmin +"$((RETENTION_HOURS * 60))" -delete
    find "$STAGE_DIR" -name 'config_*.tar.gz' -mmin +"$((RETENTION_HOURS * 60))" -delete
fi

echo "backup ${STAMP} done"
