# Sportsbetting project

News Latency Edge: measure whether Novig prices lag player news from @UnderdogNBA and
@UnderdogNFL, and take stale orders when they do. Taker-only, shadow mode before live.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync                          # Python 3.12 + dependencies
cp .env.example .env             # fill in keys locally; .env is gitignored
docker compose up -d --wait db   # Postgres + TimescaleDB on localhost:5432
uv run alembic upgrade head
```

## Checks

```sh
uv run ruff format --check . && uv run ruff check .
uv run mypy
uv run pytest
```

## Novig trade history

```sh
uv run python scripts/backfill_novig_trades.py           # fill in any missing days
uv run python scripts/backfill_novig_trades.py --verify  # also re-check files against Novig's MD5
scripts/install_daily_jobs.sh                            # run it via launchd (4:30, 16:30)
```

Raw CSVs go to `data/novig/raw/`, Parquet to `data/novig/parquet/`, rows to `novig_trades`.
Logs are in `logs/novig-trades.log`.

## Novig order-book recorder

```sh
uv run python scripts/create_read_key.py --env paper --mgmt-key-id <id> \
    --mgmt-pem <path> --out ~/.novig/paper-recorder.pem   # once; prints the read key ID
uv run python scripts/run_novig_recorder.py               # record until stopped
uv run python scripts/run_novig_recorder.py --gaps        # print windows with no data
```

Set `NOVIG_ENV`, `NOVIG_TRADING_KEY_ID`, and `NOVIG_PRIVATE_KEY_PATH` in `.env` first.
`scripts/install_daily_jobs.sh` also installs the recorder as a launchd job that restarts
after a crash and keeps the Mac awake (`caffeinate -i -s`) while it runs. Raw messages go
to `data/novig/stream/<env>/`, with every connect, disconnect, and seq gap in
`connections.jsonl` there. Logs are in `logs/novig-recorder.log`.

A nightly launchd job (03:00) archives each finished day of stream files to the
`backup.dest` folder in `config/default.yaml`, verifies every archive, and logs disk usage
to `logs/novig-backup.log`:

```sh
uv run python scripts/backup_novig_stream.py              # back up any finished day now
uv run python scripts/backup_novig_stream.py --disk-only  # just the disk-usage line
```

## Layout

- `src/news_edge/`: `core`, `sources`, `classify`, `entities`, `market`, `pricing`,
  `signal`, `execution`, `ledger`, `research`, `ui`
- `alembic/`: migrations
- `config/`: thresholds, limits, polling schedules
- `tests/unit`, `tests/replay`
- `scripts/`: backfill, kill switch, daily reports
- `docs/private/`: strategy spec (gitignored)
