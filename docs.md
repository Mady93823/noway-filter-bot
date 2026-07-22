# Project Docs — Movie Filter Bot (working name, TBD)

Full specification. Written after studying VJ-FILTER-BOT's source directly (not just its README) to understand what works, what breaks at scale, and what to do differently. This is a new build, not a fork.

---

## 1. Vision

A Telegram bot that indexes movie/show files posted to source channels and lets users find them by searching `<name> <year> <language>` (e.g. "swati 1997 tamil") — matching correctly even when the actual filename is abbreviated (`swat.1997.tam.mkv`) or missing the language entirely (language sometimes only appears in the caption). Built to index lakhs of files without the database, search, or indexing process degrading, and to survive crashes without losing indexing progress or requiring manual recovery.

No external metadata API dependency (no TMDB/IMDB API key) — all title/language/quality resolution happens from data we already have: filenames, captions, and our own growing index.

---

## 2. Constraints (explicit, decided during planning)

- No MongoDB Atlas free tier — self-hosted database only.
- No external API calls (TMDB/OMDb/etc.) in the indexing or search path.
- No self-bot/user-session automation — Bot API tokens only.
- Must handle lakhs (100,000+) of files without linear slowdown.
- Must recover automatically from a crash mid-index, without re-scanning already-completed history.
- Bot must not get flagged for abusive API usage patterns — pacing must be deliberate everywhere.

---

## 3. Current Telegram Bot API limits & policy context (verified, subject to change)

- Roughly 1 message/sec per individual chat, roughly 30 messages/sec in aggregate across chats, for a standard free bot. Telegram does not officially publish exact numbers — these are safe, conservative, community-verified defaults, and actual limits can vary with bot age/history.
- Any request can return HTTP 429 with a `retry_after` value — the only correct handling is to wait that long and retry, never blind-retry.
- A 2026 "Paid Broadcasts" feature allows bots to send up to 1,000 msg/sec by pre-funding Telegram Stars — not needed at launch, worth knowing exists if broadcast volume becomes a real bottleneck later.
- Copyright/policy risk is real and active: as of mid-2026, government pressure (e.g. India's Ministry of Information and Broadcasting) has pushed Telegram to act directly against pirated movie/OTT content, naming thousands of offending channels and explicitly including bots and admins, not just channels, as enforcement targets. Telegram has already shut down piracy-catalog bots under this kind of pressure. No architecture choice here eliminates this risk — only operational choices do (distribution model, response speed to takedown notices). This is noted so it's a conscious decision, not an oversight.

---

## 4. Architecture

Three independent services, sharing one Postgres instance and one Redis instance:

```
┌─────────────┐     ┌──────────────┐     ┌───────────────────┐
│   bot/      │     │   worker/    │     │  streaming/        │
│ (live chat, │     │ (indexing:   │     │  (Phase 2 —        │
│  search,    │     │  live + bulk │     │   web watch/       │
│  delivery)  │     │  backfill)   │     │   download links)  │
└──────┬──────┘     └──────┬───────┘     └─────────┬──────────┘
       │                   │                        │
       └─────────┬─────────┴────────────┬───────────┘
                  │                      │
             ┌────▼─────┐          ┌─────▼─────┐
             │ Postgres │          │   Redis   │
             │ (source  │          │ (cache,   │
             │ of truth)│          │ pagination│
             │          │          │ progress) │
             └──────────┘          └───────────┘
```

Why split: indexing lakhs of files is a heavy, bursty workload (filename parsing, fuzzy matching, DB writes). If it runs inside the same process as the live bot, every search request queues up behind indexing work — this was one of VJ-FILTER-BOT's core problems (single process, single event loop, everything blocking everything else).

---

## 5. Database schema

Postgres, self-hosted.

**titles**
| column | notes |
|---|---|
| id | PK |
| canonical_title | resolved locally (see §7) |
| year | int |
| languages | text[] — array, supports dual-audio |
| created_at | |

No poster/plot/cast/rating fields — those would require an external metadata API, which is explicitly out of scope. If richer metadata is wanted later without a live API dependency, the only acceptable path is a one-time bulk import of a static offline dataset (e.g. a downloadable non-commercial IMDb dataset dump) loaded locally — never a per-file live API call. Not required for launch.

**files**
| column | notes |
|---|---|
| id | PK |
| title_id | FK → titles |
| telegram_file_uid | **unique constraint** — this is what makes dedup atomic and race-free |
| raw_file_name | original filename, kept for debugging/re-parsing |
| quality | parsed label: 480p/720p/1080p/HDRip/WEB-DL/BluRay/etc. |
| file_size | bytes |
| source_channel_id | |
| source_message_id | |
| indexed_at | |

**index_progress**
| column | notes |
|---|---|
| channel_id | PK |
| last_processed_message_id | updated every batch (~50-100 messages), not every single message |
| status | running / paused / completed / errored |
| updated_at | |

This table is what makes indexing resumable. On worker startup, for every channel with an incomplete job, resume from `last_processed_message_id` automatically. No admin has to notice a crash or type a skip number.

**users / groups / filters** — normalized, one table each, indexed by id. Never one table/collection per group or per user (VJ-FILTER-BOT's referral system and local-filters implementation did this and it doesn't scale — Mongo/Postgres both have real limits on table/collection count).

---

## 6. Indexing pipeline

**Live indexing:** new post in a source channel → parse → resolve title → insert file row (unique constraint handles dedup atomically, no pre-check scan needed).

**Bulk backfill (existing channel history):**
1. Worker picks up a channel's job from `index_progress` (or starts a new one).
2. Walks history in batches, checkpointing `last_processed_message_id` after each batch.
3. Paces requests adaptively — conservative by default (see §3 limits), backs off further on any FloodWait, never a single fixed sleep as the only control.
4. On crash/restart: resume from the last checkpoint automatically. Already-processed messages that get re-touched near the checkpoint boundary are cheap to re-check (unique constraint lookup, not a table scan), so checkpoint granularity doesn't need to be perfect — "every ~50-100 messages" is enough.

**Per-file parsing (no external API):**
1. Normalize filename: strip extension, split on `.`/`_`/`-`/spaces, lowercase.
2. Extract year: 4-digit token matching 19xx/20xx.
3. Extract language(s): match tokens against a bidirectional dictionary (tamil↔tam, telugu↔tel, hindi↔hin, malayalam↔mal, kannada↔kan, english↔eng, plus common variants). Also scan the caption text with the same dictionary — captions often carry language info the filename doesn't, and results from both get merged.
4. Extract quality: match against a known quality-token list (480p, 720p, 1080p, 4K, HDRip, WEB-DL, BluRay, CAM, etc.).
5. Whatever remains after stripping year/language/quality tokens is the title guess.
6. Resolve title guess against the existing `titles` table: exact match first, then fuzzy/trigram match (handles `swat` → `Swati` type truncation) with a confidence threshold.
7. If no confident match exists, create a new `titles` row from the title guess itself — this becomes the canonical entry future truncated variants will fuzzy-match against. The index self-improves over time: the more full/clean filenames get indexed, the better the canonical title pool gets, and the better future fuzzy matches resolve.
8. Insert the `files` row linked to the resolved (or newly created) title.

---

## 7. Metadata without external APIs

Because no TMDB/IMDB API is allowed, "canonical title" isn't enriched with posters/cast/plot — it's just the cleanest available version of the title text, built from whichever indexed file/caption for that movie is most complete. Practically: when multiple files resolve to the same title, prefer the longest/most-complete raw title string encountered as the display name, rather than the first one indexed. This keeps the catalog self-improving using only data already flowing through the pipeline — no external dependency, no rate limit, no API cost, no key to leak.

---

## 8. Search

**Fast path** (covers the dominant query pattern `<name> <year> <lang>`): same parser logic as indexing — regex year, language dictionary, remainder as title — matched against the `titles` table (exact, then fuzzy/trigram).

**Result display:** one movie shown once, with all quality variants listed as options (e.g. 480p – 400MB / 720p – 900MB / 1080p – 1.8GB) — not a flat repeated list per quality like VJ-FILTER-BOT shows.

**Pagination/caching:** result sets and pagination cursors live in Redis with a TTL, never in-process Python dicts. Cursor-based pagination (not `.skip()`), so deep pages don't degrade.

**Delivery:** send the file by its existing Telegram file_id directly — no re-upload needed (this part of VJ-FILTER-BOT is already correct and gets kept).

---

## 9. Streaming/download web server (Phase 2 — not yet confirmed in scope)

If built: fix VJ-FILTER-BOT's specific bug where every link request re-posts the file into a log channel (causes unbounded channel growth) — cache the reference once instead. Use signed/expiring URLs rather than a short hash prefix as "auth." Defer building this until the core bot + indexing pipeline is solid.

---

## 10. Deployment

Docker Compose on a self-hosted VPS: `bot`, `worker`, `postgres`, `redis` as separate containers (plus `streaming` if/when built). No Heroku-style free-tier dependency, no dyno-idle keepalive hacks.

---

## 11. Build order

1. Postgres schema + resumable indexing pipeline (the foundation everything else depends on).
2. Deterministic search on top of the now-clean data.
3. Bot core: search, delivery, filters, wired to the pipeline above.
4. Streaming server, if kept in scope.
5. Fuzzy/conversational layer on top, last — it's the thinnest layer and depends on everything below being solid first.

---

## 12. Open decisions

- Final project name.
- Whether the streaming/download web server is in scope at all, or Telegram-delivery-only for v1.
- Distribution/operational model, given the copyright enforcement climate noted in §3.
