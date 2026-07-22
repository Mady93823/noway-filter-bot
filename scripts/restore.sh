#!/usr/bin/env bash
# Restore from Google Drive backup. The "wire back to old data" for a new VPS.
#
#   ./scripts/restore.sh                  # newest DB dump from Drive
#   ./scripts/restore.sh db_20260722_110000.dump
#   ./scripts/restore.sh --config         # newest dump + .env/sessions bundle
#
# Needs: docker compose stack present, rclone configured (docs/backup.md).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE_DIR="${BACKUP_DIR:-$REPO_DIR/backups}/restore"
REMOTE="${RCLONE_REMOTE:-gdrive:nowaybot-backups}"
PG_SERVICE="${PG_SERVICE:-postgres}"
PG_USER="${PG_USER:-nowaybot}"
PG_DB="${PG_DB:-nowaybot}"

WANT_CONFIG=0
DUMP_NAME=""
for arg in "$@"; do
    case "$arg" in
        --config) WANT_CONFIG=1 ;;
        *) DUMP_NAME="$arg" ;;
    esac
done

mkdir -p "$STAGE_DIR"
cd "$REPO_DIR"

if [ -z "$DUMP_NAME" ]; then
    DUMP_NAME="$(rclone lsf "$REMOTE" --include 'db_*.dump' | sort | tail -1)"
    [ -n "$DUMP_NAME" ] || { echo "ERROR: no db_*.dump found in $REMOTE"; exit 1; }
fi
echo "restoring $DUMP_NAME"
rclone copy "$REMOTE/$DUMP_NAME" "$STAGE_DIR/"

if [ "$WANT_CONFIG" -eq 1 ]; then
    CONFIG_NAME="$(rclone lsf "$REMOTE" --include 'config_*.tar.gz' | sort | tail -1)"
    if [ -n "$CONFIG_NAME" ]; then
        rclone copy "$REMOTE/$CONFIG_NAME" "$STAGE_DIR/"
        tar xzf "$STAGE_DIR/$CONFIG_NAME" -C "$REPO_DIR"
        echo "restored $CONFIG_NAME (.env + sessions)"
    else
        echo "WARN: no config bundle found in $REMOTE"
    fi
fi

docker compose up -d "$PG_SERVICE"
until docker compose exec -T "$PG_SERVICE" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; do
    sleep 2
done

# --clean --if-exists: drop and recreate objects; alembic_version travels
# inside the dump, so migration state is restored with the data.
docker compose exec -T "$PG_SERVICE" pg_restore -U "$PG_USER" -d "$PG_DB" \
    --clean --if-exists --no-owner < "$STAGE_DIR/$DUMP_NAME"

echo "restore complete. start services:  docker compose up -d"
