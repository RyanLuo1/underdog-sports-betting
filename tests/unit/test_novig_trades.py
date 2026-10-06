from datetime import UTC, datetime

import polars as pl
import pytest

from news_edge.market.novig_trades import TRADES_SCHEMA, TradesFormatError, parse_trades_csv

HEADER = (
    "timestamp,outcomeId,marketId,contractSeries,league,marketType,tradeType,legs,cost,qty,side"
)
M1 = "01a0f11e-09fe-73d0-adf7-3d27f3220107"
M2 = "01a10a37-ede9-7ff3-9984-ec23c55f7981"
O1 = "01a0f11e-09fe-73d0-adf7-3d30ea4b806a"
O2 = "01a0f11e-09fe-73d0-adf7-3d430cc111df"


def row(
    ts: str,
    market: str = M1,
    outcome: str = O1,
    cost: str = "45.5",
    qty: str = "100",
    side: str = "TAKER",
    league: str = "NFL",
    market_type: str = "MONEY",
) -> str:
    return f"{ts},{outcome},{market},Football Moneyline,{league},{market_type},STRAIGHT,1,{cost},{qty},{side}"


def parse(*rows: str, header: str = HEADER) -> pl.DataFrame:
    return parse_trades_csv("\n".join([header, *rows]).encode())


def test_schema_and_price() -> None:
    df = parse(
        row("2026-10-05T17:03:11.123Z", cost="45.5", side="TAKER"),
        row("2026-10-05T17:03:11.123Z", outcome=O2, cost="54.5", side="MAKER"),
    )
    assert df.schema == TRADES_SCHEMA
    assert df["price"].to_list() == [0.455, 0.545]
    assert df["row_num"].to_list() == [1, 2]
    assert df["side"].to_list() == ["TAKER", "MAKER"]


def test_millisecond_timestamp() -> None:
    df = parse(row("2026-10-05T17:03:11.007Z"))
    assert df["ts"][0] == datetime(2026, 10, 5, 17, 3, 11, 7000, tzinfo=UTC)


def test_whole_second_timestamp_means_zero_millis() -> None:
    df = parse(row("2026-10-05T04:12:01Z"), row("2026-10-05T04:12:01.000Z", market=M2))
    expected = datetime(2026, 10, 5, 4, 12, 1, tzinfo=UTC)
    assert df["ts"].to_list() == [expected, expected]


def test_whole_second_and_millis_rows_group_together() -> None:
    df = parse(row("2026-10-05T04:12:01Z"), row("2026-10-05T04:12:01.000Z", side="TAKER"))
    assert df["taker_count"].to_list() == [2, 2]
    assert df["ambiguous"].all()


def test_single_taker_group_is_not_ambiguous() -> None:
    ts = "2026-10-05T17:03:11.123Z"
    df = parse(
        row(ts, side="TAKER"),
        row(ts, outcome=O2, side="MAKER"),
        row(ts, outcome=O2, side="MAKER"),
    )
    assert df["taker_count"].to_list() == [1, 1, 1]
    assert not df["ambiguous"].any()


def test_two_takers_same_ms_and_market_are_ambiguous() -> None:
    ts = "2026-10-05T04:00:58.177Z"
    df = parse(
        row(ts, cost="59.5", side="TAKER"),
        row(ts, outcome=O2, cost="40.5", side="MAKER"),
        row(ts, outcome=O2, cost="41", side="TAKER"),
        row(ts, cost="59", side="MAKER"),
    )
    assert df["taker_count"].to_list() == [2, 2, 2, 2]
    assert df["ambiguous"].all()


def test_ambiguity_is_per_market_and_per_millisecond() -> None:
    df = parse(
        row("2026-10-05T04:00:58.177Z", market=M1),
        row("2026-10-05T04:00:58.177Z", market=M2),  # same ms, other market
        row("2026-10-05T04:00:58.178Z", market=M1),  # same market, next ms
    )
    assert df["taker_count"].to_list() == [1, 1, 1]
    assert not df["ambiguous"].any()


def test_empty_league_and_market_type_become_null() -> None:
    df = parse(row("2026-10-05T18:41:52.000Z", league="", market_type=""))
    assert df["league"][0] is None
    assert df["market_type"][0] is None


def test_extra_columns_are_ignored() -> None:
    df = parse(row("2026-10-05T17:03:11.123Z") + ",x", header=HEADER + ",newColumn")
    assert df.columns == list(TRADES_SCHEMA)


def test_missing_column_raises() -> None:
    header = HEADER.replace(",side", "")
    with pytest.raises(TradesFormatError, match="missing columns: \\['side'\\]"):
        parse("2026-10-05T17:03:11.123Z," + ",".join(["x"] * 9), header=header)


@pytest.mark.parametrize(
    ("bad_row", "message"),
    [
        (row("2026-10-05T17:03:11.123Z", side="BUY"), "side must be TAKER or MAKER"),
        (row("2026-10-05 17:03:11"), "timestamp"),
        (row("2026-10-05T17:03:11.12Z"), "timestamp"),
        (row("2026-10-05T17:03:11.123+00:00"), "timestamp"),
        (row("2026-10-05T17:03:11.123Z", market="7f2e"), "UUID"),
        (row("2026-10-05T17:03:11.123Z", cost="abc"), "must be numbers"),
        (row("2026-10-05T17:03:11.123Z", qty="0"), "qty must be > 0"),
    ],
)
def test_bad_rows_raise(bad_row: str, message: str) -> None:
    with pytest.raises(TradesFormatError, match=message):
        parse(row("2026-10-05T17:03:11.123Z"), bad_row)
