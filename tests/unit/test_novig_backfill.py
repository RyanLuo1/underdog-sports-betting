import hashlib
from datetime import date
from pathlib import Path

import httpx
import psycopg
import pytest

from news_edge.market import novig_backfill

HEADER = (
    "timestamp,outcomeId,marketId,contractSeries,league,marketType,tradeType,legs,cost,qty,side"
)
MARKET = "01a0f11e-09fe-73d0-adf7-3d27f3220107"
O1 = "01a0f11e-09fe-73d0-adf7-3d30ea4b806a"
O2 = "01a0f11e-09fe-73d0-adf7-3d430cc111df"

FILES = {
    "2026-10-04": "\n".join(
        [
            HEADER,
            f"2026-10-04T17:00:00Z,{O1},{MARKET},Football Moneyline,NFL,MONEY,STRAIGHT,1,45.5,100,TAKER",
            f"2026-10-04T17:00:00Z,{O2},{MARKET},Football Moneyline,NFL,MONEY,STRAIGHT,1,54.5,100,MAKER",
        ]
    ),
    "2026-10-05": "\n".join(
        [
            HEADER,
            f"2026-10-05T17:00:00.250Z,{O1},{MARKET},Football Moneyline,NFL,MONEY,STRAIGHT,1,60,100,TAKER",
            f"2026-10-05T17:00:00.250Z,{O2},{MARKET},Football Moneyline,NFL,MONEY,STRAIGHT,1,40,100,MAKER",
            f"2026-10-05T17:00:00.250Z,{O2},{MARKET},Football Moneyline,NFL,MONEY,STRAIGHT,1,41,100,TAKER",
            f"2026-10-05T17:00:00.250Z,{O1},{MARKET},Football Moneyline,NFL,MONEY,STRAIGHT,1,59,100,MAKER",
        ]
    ),
}


class FakeNovig:
    def __init__(self, files: dict[str, str] = FILES) -> None:
        self.files = files
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if request.url.path == "/":
            links = "".join(f'<a href="/reporting/trade-data/{d}/trades.csv">' for d in self.files)
            return httpx.Response(200, text=links)
        day = request.url.path.split("/")[3]
        body = self.files[day].encode()
        etag = f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"'
        return httpx.Response(200, content=body, headers={"etag": etag})


@pytest.fixture
def novig() -> FakeNovig:
    return FakeNovig()


def run(
    tmp_path: Path, novig: FakeNovig, conn: psycopg.Connection | None, verify: bool = False
) -> novig_backfill.Summary:
    with httpx.Client(transport=httpx.MockTransport(novig), base_url="https://data.novig.com") as c:
        return novig_backfill.run(tmp_path, conn, client=c, verify=verify)


def test_files_only_then_rerun_skips_everything(tmp_path: Path, novig: FakeNovig) -> None:
    first = run(tmp_path, novig, conn=None)
    assert first.downloaded == first.converted == [date(2026, 10, 4), date(2026, 10, 5)]
    assert first.failed == {}
    assert (tmp_path / "raw/trades-2026-10-05.csv").exists()
    assert (tmp_path / "parquet/trades-2026-10-05.parquet").exists()

    novig.requests.clear()
    second = run(tmp_path, novig, conn=None)
    assert second.downloaded == second.converted == []
    assert novig.requests == ["/"]


def test_one_bad_day_does_not_stop_the_rest(tmp_path: Path) -> None:
    bad = FakeNovig({**FILES, "2026-10-04": HEADER + "\nnot,a,valid,row,,,,,,,"})
    summary = run(tmp_path, bad, conn=None)
    assert list(summary.failed) == [date(2026, 10, 4)]
    assert "TradesFormatError" in summary.failed[date(2026, 10, 4)]
    assert summary.converted == [date(2026, 10, 5)]


def test_verify_refetches_corrupt_files_only(tmp_path: Path, novig: FakeNovig) -> None:
    run(tmp_path, novig, conn=None)
    corrupt = tmp_path / "raw/trades-2026-10-05.csv"
    corrupt.write_bytes(b"\0" * 10 + corrupt.read_bytes()[10:])

    summary = run(tmp_path, novig, conn=None, verify=True)
    assert summary.replaced == summary.downloaded == [date(2026, 10, 5)]
    assert summary.converted == [date(2026, 10, 5)]
    assert corrupt.read_bytes() == FILES["2026-10-05"].encode()

    assert run(tmp_path, novig, conn=None, verify=True).replaced == []


@pytest.mark.db
def test_load_and_reload_are_idempotent(
    tmp_path: Path, novig: FakeNovig, db: psycopg.Connection
) -> None:
    first = run(tmp_path, novig, db)
    assert first.loaded == [date(2026, 10, 4), date(2026, 10, 5)]

    rows = db.execute(
        "SELECT file_date, side, price, taker_count, ambiguous, ts::text "
        "FROM novig_trades ORDER BY file_date, row_num"
    ).fetchall()
    assert rows == [
        (date(2026, 10, 4), "TAKER", 0.455, 1, False, "2026-10-04 17:00:00+00"),
        (date(2026, 10, 4), "MAKER", 0.545, 1, False, "2026-10-04 17:00:00+00"),
        (date(2026, 10, 5), "TAKER", 0.6, 2, True, "2026-10-05 17:00:00.25+00"),
        (date(2026, 10, 5), "MAKER", 0.4, 2, True, "2026-10-05 17:00:00.25+00"),
        (date(2026, 10, 5), "TAKER", 0.41, 2, True, "2026-10-05 17:00:00.25+00"),
        (date(2026, 10, 5), "MAKER", 0.59, 2, True, "2026-10-05 17:00:00.25+00"),
    ]
    files = db.execute(
        "SELECT file_date, row_count, loaded_at IS NOT NULL FROM novig_trade_files "
        "ORDER BY file_date"
    ).fetchall()
    assert files == [(date(2026, 10, 4), 2, True), (date(2026, 10, 5), 4, True)]

    second = run(tmp_path, novig, db)
    assert second.loaded == []
    assert db.execute("SELECT count(*) FROM novig_trades").fetchone() == (6,)

    # Forcing a reload replaces the day's rows instead of duplicating them.
    db.execute("UPDATE novig_trade_files SET loaded_at = NULL WHERE file_date = '2026-10-05'")
    third = run(tmp_path, novig, db)
    assert third.loaded == [date(2026, 10, 5)]
    assert db.execute("SELECT count(*) FROM novig_trades").fetchone() == (6,)
