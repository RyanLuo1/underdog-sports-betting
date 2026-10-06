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
- `sources/`: news ingestion
- `classify/`: turns posts into structured news events with a rules-based parser (the
  only classifier)
- `entities/`: players, teams, and their Novig market mappings
- `market/`: Novig data. `novig_public` and `novig_trades` (daily trade files),
  `novig_backfill` (download, Parquet, load), `novig_auth` (NOVIG-V3 signing),
  `novig_keys` (subaccount and read key routes), `novig_stream` (order-book recorder)
- `pricing/`, `signal/`: fair prices and decisions
- `execution/`: orders (shadow mode only)
- `ledger/`: CLV ledger
- `research/`: analysis and notebooks
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
```

- `com.newsedge.novig-trades`: trade-file backfill at 04:30 and 16:30 local.
- `com.newsedge.novig-recorder`: order-book recorder, kept alive and run under
  `caffeinate -i -s`. It opens several connections (`recorder.connections` in
  `config/default.yaml`), since Novig caps each at 2048 markets.
- `com.newsedge.novig-backup`: 03:00 local. Archives each finished UTC day of
  `data/novig/stream/<env>/` to `backup.dest` in `config/default.yaml` (an iCloud Drive
  folder outside Desktop and Documents), verifies each archive (opens, every file reads
  back, names and sizes match), copies the gap log, and logs a disk-usage line. Verified
  days are listed in `data/novig/stream/<env>/backups.json`. Local files are never
  deleted.

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
- Use the locks. Never run two backfills or two recorders at once (`data/novig/.backfill.lock`,
  `data/novig/.recorder.lock`).

## Current status

Update this section at the end of each session.

As of 2026-10-06:

- Built: Novig trade-file backfill (17.8M rows, Aug 4 to Oct 5), NOVIG-V3 signing,
  read key script, and the order-book recorder (4 connections, gap log).
- Running: `novig-recorder` on Production (4 connections), `novig-trades` (twice
  daily), `novig-backup` (nightly to iCloud Drive). Disk was 89% used (26 GiB free) on
  Oct 6; watch the disk line in `logs/novig-backup.log`.
- Next, in order (see the Plan section of `docs/private/spec.md`):
  1. Recorder on Production: running since Oct 6. Check its gap log after the first
     NFL Sunday.
  2. Stage A as a research notebook in `src/news_edge/research/notebooks/`, not a
     service: @UnderdogNFL posts since Aug 4 (Apify backfill) joined to `novig_trades`.
     The method is in the spec, not here.
  3. Go/no-go from Stage A results, decided by the user.
  4. Only on a go: the signal engine, executor (shadow mode), and audit UI.
