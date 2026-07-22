# CLAUDE.md

Guidance for any AI agent (or human) working on this codebase. This is a **new, independent project** — not a fork of VJ-FILTER-BOT. That repo was studied purely for architecture lessons (see "What NOT to copy" below). Full spec lives in `docs.md`; this file is the fast-reference ruleset.

## What this is

A Telegram movie/file filter bot: indexes media posted to source channels, lets users search by `<name> <year> <language>`, and delivers/streams the matching file. Built to handle lakhs (100,000+) of indexed files without falling over, and to survive crashes without losing indexing progress.

## Golden rules — read before writing code

1. **No external metadata API (TMDB/IMDB/OMDb) at runtime.** Title/year/language/quality resolution happens locally, from the filename + caption + our own growing title index. No live third-party API call in the indexing or search path. (See `docs.md` → "Metadata without external APIs" for how this works.)
2. **No blocking calls inside async handlers.** All DB access is async (Motor for Mongo-style, or asyncpg/SQLAlchemy-async for Postgres). Never call sync `pymongo`, sync `requests`, or any blocking I/O from inside a coroutine.
3. **No MongoDB Atlas free tier.** Primary datastore is self-hosted Postgres. (If Mongo is used instead, it must also be self-hosted — never rely on a capped free tier.)
4. **No in-memory-only state for anything cross-request.** Search result caching, pagination cursors, indexing checkpoints, session/verification tokens — all of it goes in Redis or the DB, never a module-level Python dict. In-memory-only state is lost on restart and breaks multi-instance scaling.
5. **No self-bot / user-session automation.** Bot API tokens only, always. This is the single biggest lever against account bans.
6. **Never silently merge files as duplicates just because the name matches.** Same title, different file size = a different quality variant. Both get indexed. Dedup is only for the exact same Telegram file (enforced via a DB unique constraint, not a manual scan-before-insert).
7. **Indexing must be resumable by design.** Every channel backfill job checkpoints its last-processed message id to the database every batch (not just held in a variable). A crash or restart must resume automatically from the last checkpoint — no manual admin command required.
8. **Respect Telegram's rate limits adaptively.** ~1 msg/sec per chat, ~30 msg/sec across chats for a free bot (Telegram doesn't publish exact numbers officially — these are safe conservative defaults). On a 429/FloodWait, honor the `retry_after` value and back off further; don't just retry blindly. Don't use a single fixed `sleep()` as the only pacing mechanism.

## Architecture

Three independent services, sharing one Postgres instance and one Redis instance — no service holds state the others can't see:

- **bot/** — the live Telegram bot process. Handles search, filters, delivery, admin commands. Must stay fast; never runs indexing work itself.
- **worker/** — the indexing pipeline. Live auto-index of new channel posts + resumable bulk backfill of channel history. Filename/caption parsing, local title resolution, checkpointing.
- **streaming/** *(Phase 2 — confirm before building)* — optional web server for browser watch/download links. Only build this once the core bot + indexing is solid.
- **shared/** — DB models/schema, Redis client, config loading, the language dictionary, filename-parsing utilities. One shared module — no service should open its own separate DB connection logic.

## Data model (full schema in `docs.md`)

- `titles` — one row per resolved movie identity (title, year, languages[]). Resolved entirely from local data, no external metadata fields.
- `files` — one row per quality/size variant, foreign key to `titles`, unique constraint on the Telegram file identifier.
- `index_progress` — one row per channel being indexed: last processed message id, status, updated_at. This is what makes indexing resumable.
- `users`, `groups`, `filters` — normalized, indexed by id. Never one collection/table per entity (that was VJ-FILTER-BOT's referral-system and local-filters anti-pattern).

## Search rules

- Fast path: deterministic parse of the `<name> <year> <lang>` pattern — regex for the year, a bidirectional language dictionary (tamil↔tam, telugu↔tel, hindi↔hin, etc.) for the language, remainder is the title.
- Always search against the resolved `titles` table, never raw filenames directly — that's what the indexing pipeline's local title-resolution step is for.
- Fallback for anything that doesn't fit the pattern: fuzzy/trigram match (Postgres `pg_trgm`).

## What NOT to copy from VJ-FILTER-BOT (confirmed by reading its source)

- Synchronous `pymongo` calls inside `async def` functions (blocks the whole bot on every DB hit).
- One MongoDB collection per group / per user / per clone bot (referral system, local filters).
- Search/pagination/session state kept only in module-level Python dicts (`utils.temp`, etc.) — lost on restart, unsafe across multiple instances.
- Dedup done by repeated `find_one` scans with no index, instead of a DB-level unique constraint.
- Indexing progress (`temp.CURRENT`) held only in memory, recovered only via a manual `/setskip <number>` admin command.
- Fixed `sleep(1)` every 30 messages as the only pacing strategy — not adaptive to real FloodWait feedback.
- Re-posting the same file into a log channel on every stream/download link request (unbounded channel growth).
- Admin HTTP endpoints (`/api/admin/*`) gated only by a boolean env flag, no real authentication.
- Hardcoded API keys committed to source.
- Double plugin loading (manual importlib loop + framework's own auto-loader for the same directory).

## Before adding any new feature

Check: does it need a live external API call in a hot path? Does it add another place that opens its own DB connection? Does it introduce new in-memory-only state? If yes to any of these, stop and design it consistently with the rules above first.
