"""Parse Novig's public daily trades.csv.

Each executed trade has one TAKER row and one MAKER row per counterparty. The file has
no trade ID, so rows are grouped by (timestamp, marketId). When a group holds more than
one taker, makers cannot be paired to takers, and the group is flagged ambiguous.

Column reference: https://docs.novig.com/public-data/trades.md
"""

from pathlib import Path

import polars as pl

REQUIRED_COLUMNS = (
    "timestamp",
    "outcomeId",
    "marketId",
    "contractSeries",
    "league",
    "marketType",
    "tradeType",
    "legs",
    "cost",
    "qty",
    "side",
)

SIDES = ("TAKER", "MAKER")

_UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# Whole-second rows drop the fraction: 2026-10-05T04:12:01Z means .000.
_TS_RE = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$"

TRADES_SCHEMA = pl.Schema(
    {
        "ts": pl.Datetime("ms", "UTC"),
        "row_num": pl.Int32(),
        "outcome_id": pl.String(),
        "market_id": pl.String(),
        "contract_series": pl.String(),
        "league": pl.String(),
        "market_type": pl.String(),
        "trade_type": pl.String(),
        "legs": pl.Int16(),
        "cost": pl.Float64(),
        "qty": pl.Float64(),
        "price": pl.Float64(),
        "side": pl.String(),
        "taker_count": pl.Int16(),
        "ambiguous": pl.Boolean(),
    }
)


class TradesFormatError(ValueError):
    """The file does not match the documented trades.csv format."""


def parse_trades_csv(source: Path | bytes) -> pl.DataFrame:
    """Read one day's trades.csv into a typed frame (see TRADES_SCHEMA).

    `row_num` is the 1-based data row number in the file, which keeps rows unique even
    when two makers fill at the same price in the same millisecond.
    """
    raw = pl.read_csv(source, infer_schema=False)
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise TradesFormatError(f"trades.csv is missing columns: {missing}")
    return parse_trades(raw)


def parse_trades(raw: pl.DataFrame) -> pl.DataFrame:
    """Type and validate raw trades.csv rows, all columns read as strings."""
    _check(raw, ~pl.col("side").is_in(SIDES), "side must be TAKER or MAKER")
    _check(
        raw,
        ~pl.col("timestamp").str.contains(_TS_RE),
        "timestamp must be ISO-8601 UTC with optional milliseconds",
    )
    _check(
        raw,
        ~(pl.col("outcomeId").str.contains(_UUID_RE) & pl.col("marketId").str.contains(_UUID_RE)),
        "outcomeId and marketId must be UUIDs",
    )

    trades = raw.with_row_index("row_num", offset=1).select(
        pl.when(pl.col("timestamp").str.contains(".", literal=True))
        .then(pl.col("timestamp"))
        .otherwise(pl.col("timestamp").str.replace("Z", ".000Z", literal=True))
        .str.to_datetime("%Y-%m-%dT%H:%M:%S%.3fZ", time_unit="ms", time_zone="UTC")
        .alias("ts"),
        pl.col("row_num").cast(pl.Int32),
        pl.col("outcomeId").alias("outcome_id"),
        pl.col("marketId").alias("market_id"),
        pl.col("contractSeries").alias("contract_series"),
        _empty_to_null("league").alias("league"),
        _empty_to_null("marketType").alias("market_type"),
        pl.col("tradeType").alias("trade_type"),
        pl.col("legs").cast(pl.Int16, strict=False),
        pl.col("cost").cast(pl.Float64, strict=False),
        pl.col("qty").cast(pl.Float64, strict=False),
        pl.col("side"),
    )
    _check(
        trades,
        pl.any_horizontal(pl.col("legs", "cost", "qty").is_null()),
        "legs, cost, and qty must be numbers",
    )
    _check(trades, (pl.col("qty") <= 0) | (pl.col("cost") < 0), "qty must be > 0, cost >= 0")

    return (
        trades.with_columns(
            (pl.col("cost") / pl.col("qty")).alias("price"),
            (pl.col("side") == "TAKER")
            .sum()
            .over("ts", "market_id")
            .cast(pl.Int16)
            .alias("taker_count"),
        )
        .with_columns(
            (pl.col("taker_count") > 1).alias("ambiguous"),
        )
        .select(list(TRADES_SCHEMA))
    )


def _empty_to_null(column: str) -> pl.Expr:
    return pl.when(pl.col(column) == "").then(None).otherwise(pl.col(column))


def _check(df: pl.DataFrame, bad: pl.Expr, message: str) -> None:
    offenders = df.with_row_index("_row", offset=1).filter(bad.fill_null(True))
    if offenders.height:
        rows = offenders["_row"].head(5).to_list()
        raise TradesFormatError(f"{message} ({offenders.height} rows, first at rows {rows})")
