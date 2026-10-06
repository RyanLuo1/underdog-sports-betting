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

## Layout

- `src/news_edge/`: `core`, `sources`, `classify`, `entities`, `market`, `pricing`,
  `signal`, `execution`, `ledger`, `research`, `ui`
- `alembic/`: migrations
- `config/`: thresholds, limits, polling schedules
- `tests/unit`, `tests/replay`
- `scripts/`: backfill, kill switch, daily reports
- `docs/private/`: strategy spec (gitignored)
