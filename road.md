# road.md — how this project is planned, executed, and verified

Written at the user's request: how I work, what the model is, and how far
this bot is from production.

## 1. The model

- The work in this repo was done by **Claude Fable 5** (`claude-fable-5`),
  first model of Anthropic's Claude 5 family, in the **Mythos-class tier
  that sits above Claude Opus** in capability. Fable 5 is Anthropic's most
  intelligent generally available model. It shares its underlying model
  with Claude Mythos 5 — Mythos being the variant without the additional
  dual-use safety measures, available only to approved organizations.
  Details: https://www.anthropic.com/news/claude-fable-5-mythos-5
- Relative to the other current Claude models (Opus 4.8, Sonnet 5,
  Haiku 4.5): Fable 5 is top of the line, Opus is the previous flagship
  tier, Sonnet balances cost and speed, Haiku is the fast/cheap tier.
- Honest limit: I don't carry my own benchmark scores around and won't
  invent numbers. Judge the intelligence by the method below and by what
  actually shipped here — every claim in this file is checkable in code,
  tests, or logs.

## 2. How I plan

1. **Ground truth before code.** Read the schema, the real data, the
   actual library behavior — never assume. Before Phase 1, a live probe
   confirmed bots cannot read channel history, which forced the
   `get_messages`-walker backfill design instead of a history scan.
2. **Constraints first.** CLAUDE.md's golden rules (no blocking calls in
   async, no in-memory cross-request state, DB-level dedup, resumable
   indexing) are hard invariants; every feature is designed inside them.
3. **Phases with gates.** Core indexing → search → bot UX → hardening.
   Nothing gets built on top of an unverified layer.
4. **Decisions get recorded.** Non-obvious choices (two trigram
   thresholds, per-file vs per-title languages) go into persistent memory
   with the *why*, so a later session doesn't "simplify" them back into
   bugs.

## 3. How I execute

- Smallest correct change; one module owns one concern — `bot/ui.py` owns
  every user-visible string, `worker/pacing.py` owns rate limiting.
- Guarantees live in the database, not in application code: dedup is a
  unique constraint (not a lookup), resume state is a checkpointed row
  (not a variable).
- Failure paths are designed, not patched: FloodWait honored adaptively,
  progress reporting can never kill a backfill, expired cursors return an
  explicit "expired" page rather than silently re-running a stale query.

## 4. How I verify

- Pure logic → unit tests (**62** in `tests/`). Wiring → live smoke
  scripts against real Postgres + Redis. "Done" is claimed only after both.
- **Real data trains the code.** The parser was retrained from a full
  channel dump: 92 real filenames, old-vs-new parse diffed — 67 improved,
  0 regressions — and those real cases became permanent regression tests.
- When a smoke test surprises (seeded rows colliding with freshly
  reindexed real data), the *test* gets fixed to respect reality; real
  rows are never sacrificed to make a test pass.

## 5. Production runway

Done and verified:
- ✅ Worker: live auto-index + resumable backfill, adaptive pacing,
  progress DMs every 90s, per-batch new/already/skipped counters
- ✅ Parser: trained on real channel data, subtitle-vs-audio separation,
  per-file languages (migration 0002)
- ✅ Search: 3-rung ladder (exact → substring → typo-fuzzy), Redis-cached
  pagination, quality + language variant filtering
- ✅ Bot UX: /start menus, result cards, variant buttons, group→PM
  deep-link delivery
- ✅ Admin: /index, /help, /stats (system specs), /clear_index (confirm)
- ✅ Ops: docker-compose stack, hourly Drive backups + 7-day retention +
  VPS-migration restore (`scripts/`, `docs/backup.md`)
- ✅ Series/episode model: season joins the title identity, episode range
  labels the file (migration 0003). Real data went from one 16-variant
  "Wednesday" blob to S01 (7) + S02 (9), and six junk Dhoolpet titles to
  one series card with E09-E14. `python -m worker.reparse` applies a
  parser upgrade to an existing index without re-walking any channel.

- ✅ Two-level result UI: page of 10 titles → tap one → its files, with
  per-title audio-filter chips. A flat page put 160 buttons on one card.
- ✅ Moderation: `/ban` `/unban` `/banned` `/unbanall`, enforced on every
  entry point through a self-healing Redis mirror of `users.is_banned`
- ✅ Group bookkeeping: `groups` written on add, deactivated (never
  deleted) on removal, plus a lazy once-a-day catch-up for groups the
  bot was already in
- ✅ Group keyword filters: `/filter` `/filters` `/stop` `/stopall`,
  one shared table, Redis-cached per group, falls through to search
  when nothing matches (migration 0004)

- ✅ TgCrypto in the containers. The obvious `tgcrypto` pin cannot build
  on Python 3.12 — upstream stopped shipping wheels in 2022, so the image
  build died in gcc. `tgcrypto-pyrofork` provides the same `tgcrypto`
  module with a cp312 wheel, which beats adding a compiler to the image.
  Verified inside the running container, not just locally.
- ✅ Crash alerting and health endpoints. Admin DM on any fatal in the
  bot, the worker, the job dispatcher, or a backfill task — deduped in
  Redis so a crash loop alerts once per cooldown instead of flooding.
  `/health` on bot :8080 and worker :8081 reports Postgres and Redis
  reachability and answers 503 when either is gone, so "unhealthy" means
  "cannot do its job" rather than "process exited". Wired into
  docker-compose healthchecks; all four services report healthy.

- ✅ Lakh-scale rehearsal (`scripts/loadtest.py`). 100,000 titles /
  250,000 files seeded at ~6,600 rows/s; 122 MB database, 9.7 MB trigram
  index. Search after `ANALYZE`, cold Redis, so the numbers are Postgres
  alone: substring p50 17 ms / p95 132 ms, typo-fuzzy p50 68 ms / p95
  97 ms. The exact-match rung itself is an index scan at 0.08 ms.
  It found one real problem: the trigram `%` operator tests against
  Postgres' own `pg_trgm.similarity_threshold` (0.3), not against the
  threshold we then filter on, so the indexer's 0.45 lookup was making
  the GIN index return every candidate above 0.3 and throwing most away
  in the recheck. Aligning the two per transaction halves that query
  (391 ms → 186 ms on an identical result set) — and it runs once per
  indexed file, so a 250k-file backfill saves roughly half its database
  time. Search deliberately keeps 0.3 and is unaffected.
  Caveat worth stating: the synthetic corpus is built from 14 words with
  a shared prefix on every row, which is far more trigram-hostile than
  real titles. The 17–68 ms rungs are representative; a 425 ms figure
  appears only when a query token occurs in 100% of the corpus.
  Not rehearsed: Telegram itself — FloodWait and `get_messages` pacing
  need a real channel and can only be measured for real.

Remaining before calling it production:
1. ⬜ Streaming/watch-online server — Phase 4 in docs.md, deferred;
   docs.md §12 still lists "is it in scope at all" as open.

That one is a product decision, not a deploy task.
