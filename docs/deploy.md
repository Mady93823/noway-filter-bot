# Deploying to a VPS

From a bare Ubuntu box to a running bot with hourly backups. Roughly 15
minutes, most of it waiting for Docker to install.

Backup and restore detail lives in [backup.md](backup.md); this file
covers first deployment and day-to-day operation.

---

## 1. Prerequisites on the VPS

```bash
# Docker + compose plugin
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # log out and back in after this
sudo apt-get update && sudo apt-get install -y git rclone
```

2 GB RAM is enough for the bot, the worker, Postgres and Redis together.
Indexing is I/O bound, not CPU bound.

---

## 2. Clone and configure

```bash
sudo mkdir -p /opt && cd /opt
git clone https://github.com/Mady93823/noway-filter-bot.git nowaybot
cd nowaybot
cp .env.example .env
nano .env
```

Fill in the four things that have no sensible default:

| Variable | Where it comes from |
|---|---|
| `API_ID`, `API_HASH` | https://my.telegram.org → API development tools |
| `BOT_TOKEN` | @BotFather |
| `ADMIN_IDS` | your Telegram numeric id (@userinfobot tells you) |
| `SOURCE_CHANNEL_IDS` | the channel(s) to index; the bot must be **admin** in each |

Leave `DATABASE_URL` and `REDIS_URL` alone — compose overrides them with
the internal service hostnames.

**`.env` is gitignored and must stay that way.** It holds your bot token,
and that token is what keeps every indexed `file_id` sendable — losing or
leaking it is the one unrecoverable mistake here.

---

## 3. First run

```bash
docker compose up -d postgres redis
docker compose run --rm worker alembic upgrade head   # creates the schema
docker compose up -d
```

Verify before going further:

```bash
docker compose ps                    # all four "healthy"
curl -s localhost:8080/health        # {"service":"bot","ok":true,...}
curl -s localhost:8081/health        # worker
docker compose logs --tail=30 bot
```

`ok:false` means the service cannot reach Postgres or Redis — the
endpoint answers 503 in that case, so "healthy" genuinely means "can do
its job", not merely "the process is alive".

The health, Postgres and Redis ports are bound to `127.0.0.1` on
purpose. Postgres here has a default password and Redis has none; do not
change those bindings to `0.0.0.0` on a public box. To watch health from
outside, put it behind a reverse proxy instead of opening the port.

---

## 4. Configure from Telegram

These live in the database, not `.env`, so they take effect immediately
in both services with no restart. DM the bot as an admin:

```
/setlog -1001234567890     # posts a test message before saving
/setshortener <api_token>  # AroLinks etc. Your message is auto-deleted
/setaccesshours 4          # how long one unlock lasts
/shortlink on              # switch the gate on (refused with no token)
/showconfig                # confirm; the token is masked
/help                      # full command reference
```

Then start indexing: forward the source channel's latest post to the bot
in PM, or `/index <channel_id> <last_message_id>`. The worker resumes
automatically after a crash or restart — there is no manual resume step.

---

## 5. Hourly backups, 7-day retention

The script is already in the repo. It dumps Postgres, bundles `.env` +
`sessions/`, uploads to Google Drive, and prunes anything older than 7
days **on both sides**.

```bash
chmod +x scripts/*.sh
rclone config      # once: new remote named "gdrive", type "drive"
```

`rclone config` wants a browser. On a headless VPS answer **N** to auto
config and follow the `rclone authorize` instructions it prints.

Add the cron entry:

```bash
crontab -e
# add this line:
0 * * * * cd /opt/nowaybot && ./scripts/backup.sh >> backups/backup.log 2>&1
```

Check it works before trusting it:

```bash
./scripts/backup.sh          # run once by hand
ls -la backups/              # db_*.dump and config_*.tar.gz
rclone ls gdrive:nowaybot-backups
```

Retention is `RETENTION_DAYS` (default 7) and applies to both local
staging and Drive. Without rclone configured the script still keeps
local backups and warns — a missing Drive setup never means "no backup
at all".

**Restoring is the step people skip and regret.** Do it once now, while
nothing is at stake: `backup.md` has the drill.

---

## 6. Updating

```bash
cd /opt/nowaybot
git pull
docker compose run --rm worker alembic upgrade head   # if migrations changed
docker compose up -d --build
docker compose ps
```

Run `alembic upgrade head` before `up -d --build` when a release adds a
migration: the new code may expect columns the old schema does not have.

---

## 7. Operational notes

- **Logs**: `docker compose logs -f bot` / `worker`. Fatal errors also DM
  every admin and post to the log channel, deduped so a crash loop is one
  message per cooldown rather than thousands.
- **Moving VPS**: restore the config bundle, not just the database. The
  same `BOT_TOKEN` is what keeps indexed `file_id`s valid — a different
  token means re-indexing everything. Full runbook in `backup.md`.
- **Session files**: `sessions/` is a bind mount and is gitignored. It
  contains auth keys; treat it exactly like `.env`.
- **Firewall**: only SSH needs to be open. The bot dials out to Telegram;
  nothing needs to reach it inbound.
