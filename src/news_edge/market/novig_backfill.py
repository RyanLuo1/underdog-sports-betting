"""Backfill Novig's public trades: download raw CSVs, convert to Parquet, load Postgres.

Every step is idempotent, so the same run works for the first full backfill and for the
daily job: it fills whatever is missing for every date Novig still lists.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import polars as pl
import psycopg

from news_edge.market import novig_public
from news_edge.market.novig_trades import TRADES_SCHEMA, parse_trades_csv

log = logging.getLogger(__name__)

_DB_COLUMNS = ["file_date", *TRADES_SCHEMA]
_COPY_CHUNK_ROWS = 200_000


@dataclass
class Summary:
    downloaded: list[date] = field(default_factory=list)
    converted: list[date] = field(default_factory=list)
    loaded: list[date] = field(default_factory=list)
    replaced: list[date] = field(default_factory=list)
    failed: dict[date, str] = field(default_factory=dict)
    gaps: list[date] = field(default_factory=list)


def parquet_path(parquet_dir: Path, day: date) -> Path:
    return parquet_dir / f"trades-{day.isoformat()}.parquet"


def ensure_parquet(raw: Path, dest: Path) -> pl.DataFrame | None:
    """Convert raw CSV to Parquet if the Parquet file is missing or older than the CSV.
    Returns the parsed frame when it converted, else None."""
    if dest.exists() and dest.stat().st_mtime >= raw.stat().st_mtime:
        return None
    trades = parse_trades_csv(raw)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".parquet.part")
    trades.write_parquet(tmp, compression="zstd")
    tmp.rename(dest)
    return trades


def record_file(conn: psycopg.Connection, day: date, raw: Path, etag: str | None) -> None:
    """Note a raw file in novig_trade_files. A re-download clears loaded_at."""
    conn.execute(
        """
        INSERT INTO novig_trade_files (file_date, etag, size_bytes, downloaded_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (file_date) DO UPDATE SET
            etag = COALESCE(EXCLUDED.etag, novig_trade_files.etag),
            size_bytes = EXCLUDED.size_bytes,
            downloaded_at = EXCLUDED.downloaded_at,
            loaded_at = CASE
                WHEN novig_trade_files.size_bytes = EXCLUDED.size_bytes
                THEN novig_trade_files.loaded_at
            END
        """,
        (day, etag, raw.stat().st_size, datetime.fromtimestamp(raw.stat().st_mtime, tz=UTC)),
    )


def is_loaded(conn: psycopg.Connection, day: date) -> bool:
    row = conn.execute(
        "SELECT loaded_at IS NOT NULL FROM novig_trade_files WHERE file_date = %s", (day,)
    ).fetchone()
    return bool(row and row[0])


def load_day(conn: psycopg.Connection, day: date, trades: pl.DataFrame) -> int:
    """Replace one day's public_csv rows in a single transaction."""
    frame = trades.with_columns(pl.lit(day).alias("file_date")).select(_DB_COLUMNS)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM novig_trades WHERE source = 'public_csv' AND file_date = %s", (day,)
        )
        columns = ", ".join(_DB_COLUMNS)
        with cur.copy(f"COPY novig_trades ({columns}) FROM STDIN WITH (FORMAT csv)") as copy:
            for chunk in frame.iter_slices(_COPY_CHUNK_ROWS):
                copy.write(
                    chunk.write_csv(include_header=False, datetime_format="%Y-%m-%dT%H:%M:%S%.3fZ")
                )
        cur.execute(
            "UPDATE novig_trade_files SET row_count = %s, loaded_at = now() WHERE file_date = %s",
            (frame.height, day),
        )
    return frame.height


def run(
    data_dir: Path,
    conn: psycopg.Connection | None,
    since: date | None = None,
    client: httpx.Client | None = None,
    verify: bool = False,
) -> Summary:
    """Fill in every listed day. With verify, also compare each local raw file's MD5 to
    Novig's current ETag and re-fetch any that differ (corrupt, or republished)."""
    raw_dir, parquet_dir = data_dir / "raw", data_dir / "parquet"
    summary = Summary()
    own_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(30, read=120), follow_redirects=True)
    try:
        days = novig_public.fetch_trade_days(http)
        summary.gaps = novig_public.missing_days(days)
        if summary.gaps:
            log.warning("Novig's index skips %d days: %s", len(summary.gaps), summary.gaps)
        if since:
            days = [d for d in days if d >= since]

        for day in days:
            try:
                _backfill_day(http, conn, day, raw_dir, parquet_dir, summary, verify)
            except Exception as exc:
                log.exception("%s failed", day)
                summary.failed[day] = f"{type(exc).__name__}: {exc}"
    finally:
        if own_client:
            http.close()
    return summary


def _backfill_day(
    http: httpx.Client,
    conn: psycopg.Connection | None,
    day: date,
    raw_dir: Path,
    parquet_dir: Path,
    summary: Summary,
    verify: bool,
) -> None:
    raw = novig_public.raw_path(raw_dir, day)
    parquet = parquet_path(parquet_dir, day)
    if not raw.exists() and parquet.exists():
        # The raw CSV was backed up and deleted locally (scripts/backup_novig_raw.py).
        # The Parquet copy is the local source; never download the CSV again for it.
        if conn is not None and not is_loaded(conn, day):
            # A fresh database has no file row; size 0 marks "raw CSV only in the backup".
            conn.execute(
                "INSERT INTO novig_trade_files (file_date, size_bytes, downloaded_at)"
                " VALUES (%s, 0, now()) ON CONFLICT (file_date) DO NOTHING",
                (day,),
            )
            rows = load_day(conn, day, pl.read_parquet(parquet))
            summary.loaded.append(day)
            log.info("%s: loaded %d rows from Parquet (raw CSV is in the backup)", day, rows)
        return
    etag = None
    if verify and raw.exists():
        expected = novig_public.md5_from_etag(novig_public.remote_etag(http, day))
        if expected is not None and novig_public.file_md5(raw) != expected:
            log.warning("%s: local file does not match Novig's ETag; re-fetching", day)
            raw.unlink()
            parquet_path(parquet_dir, day).unlink(missing_ok=True)
            summary.replaced.append(day)
    if not raw.exists():
        etag = novig_public.download_trades(http, day, raw_dir).etag
        summary.downloaded.append(day)

    trades = ensure_parquet(raw, parquet_path(parquet_dir, day))
    if trades is not None:
        summary.converted.append(day)

    if conn is None:
        return
    with conn.transaction():
        record_file(conn, day, raw, etag)
    if trades is not None or not is_loaded(conn, day):
        if trades is None:
            trades = pl.read_parquet(parquet_path(parquet_dir, day))
        rows = load_day(conn, day, trades)
        summary.loaded.append(day)
        log.info("%s: loaded %d rows", day, rows)
