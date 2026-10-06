import hashlib
from datetime import date
from pathlib import Path

import httpx
import pytest

from news_edge.market import novig_public

INDEX = """
<a href="https://data.novig.com/reporting/trade-data/2026-10-05/trades.csv">trades</a>
<a href="https://data.novig.com/reporting/trade-data/2026-10-05/markets.csv">markets</a>
<a href="https://data.novig.com/reporting/trade-data/2026-10-02/trades.csv">trades</a>
<a href="https://data.novig.com/reporting/trade-data/2026-10-04/trades.csv">trades</a>
"""


def test_list_trade_days_sorted_and_deduped() -> None:
    assert novig_public.list_trade_days(INDEX + INDEX) == [
        date(2026, 10, 2),
        date(2026, 10, 4),
        date(2026, 10, 5),
    ]


def test_missing_days() -> None:
    days = novig_public.list_trade_days(INDEX)
    assert novig_public.missing_days(days) == [date(2026, 10, 3)]
    assert novig_public.missing_days([]) == []


def _client(body: bytes, content_length: int | None = None, etag: str = '"abc"') -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"etag": etag}
        if content_length is not None:
            headers["content-length"] = str(content_length)
        return httpx.Response(200, content=body, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_writes_file_atomically(tmp_path: Path) -> None:
    with _client(b"a,b\n1,2\n") as client:
        dl = novig_public.download_trades(client, date(2026, 10, 5), tmp_path)
    assert dl.path == tmp_path / "trades-2026-10-05.csv"
    assert dl.path.read_bytes() == b"a,b\n1,2\n"
    assert dl.etag == '"abc"'
    assert not list(tmp_path.glob("*.part"))


def test_truncated_download_leaves_no_file(tmp_path: Path) -> None:
    with _client(b"a,b\n", content_length=999) as client, pytest.raises(RuntimeError):
        novig_public.download_trades(client, date(2026, 10, 5), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_md5_etag_is_verified(tmp_path: Path) -> None:
    body = b"a,b\n1,2\n"
    good = f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"'
    with _client(body, etag=good) as client:
        assert novig_public.download_trades(client, date(2026, 10, 5), tmp_path).etag == good

    bad = f'"{"0" * 32}"'
    with _client(body, etag=bad) as client, pytest.raises(RuntimeError, match="MD5"):
        novig_public.download_trades(client, date(2026, 10, 6), tmp_path)
    assert not (tmp_path / "trades-2026-10-06.csv").exists()
    assert not list(tmp_path.glob("*.part"))
