# Backup & VPS-migration runbook

Hourly Postgres dump + config bundle to Google Drive via rclone.
Retention: 7 days on Drive and locally. Redis is NOT backed up — it holds
only rebuildable cache (search results, cursors).

## What gets backed up

| Artifact | Contains | Why it matters |
|---|---|---|
| `db_<UTC-stamp>.dump` | full Postgres (titles, files, progress, users) | the index itself |
| `config_<UTC-stamp>.tar.gz` | `.env` + `sessions/` | bot identity — same `BOT_TOKEN` keeps every stored `telegram_file_id` sendable |

The second artifact is what makes monthly VPS hopping safe: `file_id`s are
bot-scoped, so as long as `.env` (token) survives, the whole index stays valid.

## One-time setup (per Google account, not per VPS)

1. Install rclone: `curl https://rclone.org/install.sh | sudo bash`
2. `rclone config` → `n`ew remote → name `gdrive` → storage `drive` →
   accept defaults → browser auth (on a headless VPS rclone prints a link;
   or run config on any machine and copy `~/.config/rclone/rclone.conf`).
3. Test: `rclone mkdir gdrive:nowaybot-backups && rclone lsd gdrive:`

`rclone.conf` is the single portable credential — keep a copy somewhere
safe (password manager). New VPS = paste that file, done.

## Enable hourly backups (VPS)

```bash
chmod +x scripts/backup.sh scripts/restore.sh
crontab -e
# add:
0 * * * * cd /opt/nowaybot && ./scripts/backup.sh >> backups/backup.log 2>&1
```

Manual run anytime: `./scripts/backup.sh`
Overrides via env: `RCLONE_REMOTE`, `RETENTION_DAYS`, `BACKUP_DIR`,
`PG_SERVICE/PG_USER/PG_DB`.

Retention math: hourly × 7 days = max 168 dumps. A lakh-scale index dump
(custom format, compressed) is tens of MB — comfortably inside free Drive.

## Monthly VPS switch runbook

On the NEW VPS:

```bash
# 1. deps
apt install -y docker.io docker-compose-plugin git
curl https://rclone.org/install.sh | sudo bash

# 2. identity: restore rclone.conf (from password manager / old VPS)
mkdir -p ~/.config/rclone && nano ~/.config/rclone/rclone.conf

# 3. code
git clone <repo> /opt/nowaybot && cd /opt/nowaybot

# 4. data + identity back from Drive (newest dump + .env + sessions)
./scripts/restore.sh --config

# 5. up
docker compose up -d
crontab -e   # re-add the hourly backup line
```

Old VPS can be destroyed after step 5 verifies (`/stats` in the bot).

Point-in-time restore: `rclone lsf gdrive:nowaybot-backups` → pick a stamp →
`./scripts/restore.sh db_20260722_110000.dump`.

## Verify a backup actually restores (do this once)

```bash
./scripts/backup.sh
docker compose exec -T postgres psql -U nowaybot -c "SELECT count(*) FROM files"
./scripts/restore.sh        # restores the dump you just made
docker compose exec -T postgres psql -U nowaybot -c "SELECT count(*) FROM files"
# counts must match
```
