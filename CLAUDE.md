# CLAUDE.md

This repo is public. Never write strategy details, thresholds, or edge hypotheses here,
in commits, or anywhere else that gets committed. They live only in
`docs/private/spec.md` (gitignored), which is the source of truth for the plan.

## Project

`news_edge` ingests player news, records Novig market data (the public daily trade files
and the live order-book stream), and keeps a closing-line-value (CLV) ledger of
decisions, so it can measure how Novig prices respond to news. It runs on one Mac:
Postgres with TimescaleDB in Docker, and Python jobs under launchd.

## Location and layout

The repo is at `~/code/underdog-sports-betting`. Never put it, its `.venv`, or `data/`
under an iCloud-synced folder (`~/Desktop`, `~/Documents`). iCloud hid `.venv` files
and broke imports. The launchd jobs hard-code this path, so rerun
`scripts/install_daily_jobs.sh` after any move.

`src/news_edge/`:

- `core/`: settings (`.env`), database connections, clock (t0 from post IDs)
- `sources/`: news ingestion. `apify` (actor runs, token in a header only),
  `underdog_backfill` (weekly post backfill with a cost ledger and budget stop)
- `classify/`: `rules` (the only classifier; versioned, never guesses), `store`
- `entities/`: `teams` (nicknames, abbreviations, aliases), `resolver` (post to Novig
  game and markets from the schedule, using only what was known at t0), `store`
- `market/`: Novig data. `novig_public` and `novig_trades` (daily trade files),
  `novig_backfill` (download, Parquet, load), `novig_auth` (NOVIG-V3 signing),
  `novig_keys` (subaccount and read key routes), `novig_stream` (order-book recorder),
  `novig_history` (event and market names from the signed history API)
- `pricing/`, `signal/`: fair prices and decisions
- `execution/`: orders (shadow mode only)
- `ledger/`: CLV ledger
- `research/`: `stage_a` (tested logic) and `notebooks/stage_a.ipynb` (generated, committed
  without outputs; executed results go to `docs/private/stage_a_results.ipynb`)
- `ui/`: dashboard

Also: `alembic/` (migrations), `config/default.yaml` (limits and schedules), `scripts/`
(backfill, recorder, key setup, launchd templates), `tests/unit`, `tests/replay`,
`data/` and `logs/` (gitignored).

## Commands

```sh
uv sync                                  # Python 3.12 and dependencies
cp .env.example .env                     # then fill in keys; .env is gitignored
docker compose up -d --wait db           # Postgres + TimescaleDB on localhost:5432
uv run alembic upgrade head              # migrate; new revision: uv run alembic revision -m "..."
uv run pytest                            # tests; DB tests skip if Postgres is down
uv run ruff format . && uv run ruff check .
uv run mypy                              # strict
uv sync --all-groups                     # also the research group (notebook tooling)
```

Stage A pipeline, in order (each step is rerunnable):

```sh
uv run python scripts/backfill_underdog_posts.py      # posts via Apify; $10 budget ledger
uv run python scripts/backfill_underdog_posts.py --report
uv run python scripts/classify_posts.py               # rules parser -> classifications
uv run python scripts/sync_novig_history.py --since 2026-07-25   # names; ~1 h (throttled)
uv run python scripts/resolve_entities.py             # -> entity_resolutions; logs/entity-resolution.log
uv run --group research python scripts/build_stage_a_notebook.py --run   # results -> docs/private/
```

launchd jobs (`scripts/install_daily_jobs.sh` installs every template in
`scripts/launchd/`; `--uninstall` removes them):

```sh
launchctl list | grep newsedge                                # loaded jobs, PID, last exit
launchctl print gui/$(id -u)/com.newsedge.novig-recorder      # state and run count
launchctl kickstart -k gui/$(id -u)/com.newsedge.novig-recorder   # restart, e.g. after editing .env
tail -f logs/novig-recorder.log logs/novig-trades.log logs/novig-backup.log
uv run python scripts/run_novig_recorder.py --gaps            # windows with no stream data
uv run python scripts/backup_novig_stream.py --disk-only      # disk usage line now
uv run python scripts/backup_novig_raw.py                     # back up raw trade CSVs, verified
uv run python scripts/backup_novig_raw.py --delete-verified   # then asks before deleting any
```

- `com.newsedge.novig-trades`: trade-file backfill at 04:30 and 16:30 local.
- `com.newsedge.novig-recorder`: order-book recorder, kept alive and run under
  `caffeinate -i -s`. It opens several connections (`recorder.connections` in
  `config/default.yaml`), since Novig caps each at 2048 markets. It checks free disk
  every 2 minutes and below 5 GiB stops cleanly (logged as a gap, with a notification).
  It then stays down until restarted with `launchctl kickstart -k`.
- `com.newsedge.novig-backup`: 03:00 local. Archives each finished UTC day of
  `data/novig/stream/<env>/` to `backup.dest` in `config/default.yaml` (an iCloud Drive
  folder outside Desktop and Documents), verifies each archive (opens, every file reads
  back, names, sizes, and MD5s match), copies the gap log, and logs a disk-usage line
  with free iCloud storage (`brctl quota`). The line says WARNING below 20 GiB free and
  CRITICAL below 10 GiB, each with a macOS notification. Verified days are listed in
  `data/novig/stream/<env>/backups.json`. Local files are never deleted.

`scripts/install_daily_jobs.sh` leaves a loaded job alone when its plist is unchanged,
so rerunning it does not restart the recorder.

## Conventions

- Python 3.12. Async for network services such as the recorder. One-shot scripts may
  be synchronous.
- pydantic for config, settings, and records. mypy strict, ruff clean.
- Timestamps are UTC with millisecond precision: `timestamptz` in Postgres, Unix
  milliseconds in raw data.
- A news item's t0 is decoded from its post ID (`core/clock.py`), never from when we saw
  it.
- Every module has tests. Network tests use mock transports or a local fake server.

## Hard rules

- Never commit secrets. Keys load from `.env` through `core/settings.py`, and `.pem`
  files live outside the repo.
- Never read, store, or use the Novig management key. The user runs
  `scripts/create_read_key.py` themselves, and only that script takes it, as a
  command-line path.
- Point-in-time only. No signal may use data stamped after its decision time.
- Never place live orders. Execution stays in shadow mode until the user says otherwise.
- Do not build the signal engine, executor, or audit UI until Stage A results are
  reviewed and approved.
- Never commit research results (executed notebooks, result tables) to this public repo;
  they go in `docs/private/`.
- Research code reports ex-post measurements (prices after the analyzed moment) only in
  columns labeled as such, and never feeds them to signal code or point-in-time views.
- Never delete data without asking the user first. `backup_novig_raw.py
  --delete-verified` asks before deleting, and keeps any CSV whose backup is not on
  this Mac or that has no Parquet copy. The trades job never downloads a deleted raw
  CSV again; its Parquet copy is the local source.
- Use the locks. Never run two backfills or two recorders at once (`data/novig/.backfill.lock`,
  `data/novig/.recorder.lock`).

## Current status

Update this section at the end of each session.

As of 2026-10-06:

- Built: Novig trade-file backfill; order-book recorder (4 connections, gap log, disk
  guard); nightly backups to iCloud Drive; @UnderdogNFL post backfill (Aug 4 to Oct 6,
  3,158 posts, $4.43 of the $10 Apify budget); rules classifier; schedule-based entity
  resolution; Novig event and market names (history API); Stage A notebook.
- Running: `novig-recorder` on Production, `novig-trades` (twice daily),
  `novig-backup` (nightly). Disk was 89% used (27 GiB free) on Oct 6.
- Known data gap: no posts on Aug 31 (ET) in two separate fetches; unverified.
- Next, in order (see the Plan in `docs/private/spec.md`):
  1. Review Stage A results (`docs/private/stage_a_results.ipynb`) with the user.
  2. Go/no-go from Stage A, decided by the user.
  3. Only on a go: the signal engine, executor (shadow mode), and audit UI.
