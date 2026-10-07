"""Sync Novig events and markets (names, types, outcomes) from the signed history API.

The trade files carry only IDs. `/v3/history/events` and `/v3/history/markets` list
every event and market, newest first by ID (UUIDv7, so the ID encodes creation time),
with no league filter. The sync pages back until IDs are older than a cutoff and keeps
only events in the wanted leagues and their markets.
"""

import logging
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb

from news_edge.market.novig_auth import Signer

log = logging.getLogger(__name__)

PAGE = 5000


def uuid7_time(value: str) -> datetime:
    """Creation time encoded in a UUIDv7's first 48 bits."""
    ms = uuid.UUID(value).int >> 80
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def pages(
    http: httpx.Client, signer: Signer, path: str, stop_before: datetime
) -> Iterator[list[dict[str, Any]]]:
    """Pages of items, newest first, until an item is older than stop_before."""
    after: str | None = None
    while True:
        query = f"limit={PAGE}" + (f"&after={after}" if after else "")
        resp = http.get(f"{path}?{query}", headers=signer.headers("GET", path, query))
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", "2"))
            log.info("%s: throttled, waiting %.0f s", path, wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        body = resp.json()
        items: list[dict[str, Any]] = body["items"]
        id_key = "eventId" if path.endswith("events") else "marketId"
        fresh = [i for i in items if uuid7_time(i[id_key]) >= stop_before]
        yield fresh
        after = body.get("next")
        if not after or len(fresh) < len(items):
            return


@dataclass
class SyncSummary:
    events_seen: int = 0
    events_kept: int = 0
    markets_seen: int = 0
    markets_kept: int = 0


def sync(
    http: httpx.Client,
    signer: Signer,
    conn: psycopg.Connection,
    leagues: list[str],
    stop_before: datetime,
) -> SyncSummary:
    summary = SyncSummary()
    for page in pages(http, signer, "/v3/history/events", stop_before):
        summary.events_seen += len(page)
        keep = [e for e in page if e.get("league") in leagues]
        summary.events_kept += len(keep)
        _upsert_events(conn, keep)
    wanted = {
        str(r[0])
        for r in conn.execute(
            "SELECT event_id FROM novig_events WHERE league = ANY(%s)", (leagues,)
        )
    }
    for page in pages(http, signer, "/v3/history/markets", stop_before):
        summary.markets_seen += len(page)
        keep = [m for m in page if m["eventId"] in wanted]
        summary.markets_kept += len(keep)
        _upsert_markets(conn, keep)
        log.info(
            "markets: %d seen, %d kept (back to %s)",
            summary.markets_seen,
            summary.markets_kept,
            uuid7_time(page[-1]["marketId"]).date() if page else "-",
        )
    return summary


def _upsert_events(conn: psycopg.Connection, events: list[dict[str, Any]]) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO novig_events (event_id, sport, league, status, description, starts_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO UPDATE SET
                status = EXCLUDED.status, description = EXCLUDED.description,
                starts_at = EXCLUDED.starts_at, synced_at = now()
            """,
            [
                (
                    e["eventId"],
                    e.get("sport"),
                    e.get("league"),
                    e["status"],
                    e["description"],
                    datetime.fromtimestamp(e["startsTs"] / 1000, tz=UTC)
                    if e.get("startsTs")
                    else None,
                )
                for e in events
            ],
        )


def _upsert_markets(conn: psycopg.Connection, markets: list[dict[str, Any]]) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO novig_markets (market_id, event_id, market_type, strike, status,
                                       description, outcomes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (market_id) DO UPDATE SET
                status = EXCLUDED.status, description = EXCLUDED.description,
                outcomes = EXCLUDED.outcomes, synced_at = now()
            """,
            [
                (
                    m["marketId"],
                    m["eventId"],
                    m["marketType"],
                    m.get("strike"),
                    m["status"],
                    m["description"],
                    Jsonb(m["outcomes"]),
                )
                for m in markets
            ],
        )
