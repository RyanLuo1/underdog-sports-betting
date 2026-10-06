"""Download Novig's public daily trade files from data.novig.com.

Files are static and published once a day (around 09:00 UTC) for the previous
U.S. Eastern trading day. The index page lists every available date.
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

INDEX_URL = "https://data.novig.com/"
TRADES_URL = "https://data.novig.com/reporting/trade-data/{day}/trades.csv"

# Novig's CDN ETag is the file's MD5 (for single-part uploads: 32 hex digits, no "-").
_MD5_ETAG = re.compile(r'^"?([0-9a-f]{32})"?$')
_TRADES_LINK = re.compile(r"/reporting/trade-data/(\d{4}-\d{2}-\d{2})/trades\.csv")


@dataclass(frozen=True)
class Download:
    day: date
    path: Path
    etag: str | None
    size: int


def list_trade_days(index_html: str) -> list[date]:
    """Return every date that has a trades.csv link on the index page, oldest first."""
    return sorted({date.fromisoformat(d) for d in _TRADES_LINK.findall(index_html)})


def fetch_trade_days(client: httpx.Client) -> list[date]:
    resp = client.get(INDEX_URL)
    resp.raise_for_status()
    days = list_trade_days(resp.text)
    if not days:
        raise RuntimeError("no trades.csv links found on the Novig data index page")
    return days


def raw_path(raw_dir: Path, day: date) -> Path:
    return raw_dir / f"trades-{day.isoformat()}.csv"


def download_trades(client: httpx.Client, day: date, raw_dir: Path) -> Download:
    """Stream one day's trades.csv to raw_dir. Writes to a .part file, checks size and
    MD5 against the response headers, then renames, so a bad download never looks complete."""
    dest = raw_path(raw_dir, day)
    part = dest.with_suffix(".csv.part")
    raw_dir.mkdir(parents=True, exist_ok=True)
    with client.stream("GET", TRADES_URL.format(day=day.isoformat())) as resp:
        resp.raise_for_status()
        expected = resp.headers.get("content-length")
        etag = resp.headers.get("etag")
        md5 = hashlib.md5(usedforsecurity=False)
        with part.open("wb") as f:
            for chunk in resp.iter_bytes(1 << 20):
                f.write(chunk)
                md5.update(chunk)
    size = part.stat().st_size
    if expected is not None and size != int(expected):
        part.unlink()
        raise RuntimeError(f"{day}: got {size} bytes, expected {expected}")
    expected_md5 = md5_from_etag(etag)
    if expected_md5 is not None and expected_md5 != md5.hexdigest():
        part.unlink()
        raise RuntimeError(f"{day}: MD5 {md5.hexdigest()} does not match ETag {etag}")
    part.rename(dest)
    log.info("downloaded %s (%.1f MB)", dest.name, size / 1e6)
    return Download(day=day, path=dest, etag=etag, size=size)


def md5_from_etag(etag: str | None) -> str | None:
    """The MD5 an ETag encodes, or None if this ETag is not a plain MD5."""
    match = _MD5_ETAG.match(etag or "")
    return str(match.group(1)) if match else None


def file_md5(path: Path) -> str:
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            md5.update(chunk)
    return md5.hexdigest()


def remote_etag(client: httpx.Client, day: date) -> str | None:
    resp = client.head(TRADES_URL.format(day=day.isoformat()))
    resp.raise_for_status()
    etag: str | None = resp.headers.get("etag")
    return etag


def missing_days(days: list[date]) -> list[date]:
    """Calendar days between the first and last listed day that are not listed."""
    if not days:
        return []
    listed = set(days)
    first, last = days[0], days[-1]
    return [
        date.fromordinal(n)
        for n in range(first.toordinal(), last.toordinal() + 1)
        if date.fromordinal(n) not in listed
    ]
