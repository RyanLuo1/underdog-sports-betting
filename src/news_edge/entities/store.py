"""Resolve stored classifications to Novig events and traded markets."""

import json
import logging
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

import psycopg

from news_edge.entities.resolver import Event, Index, Market, normalize, resolve
from news_edge.entities.teams import NICKNAME_OF_ABBR

log = logging.getLogger(__name__)


def load_index(conn: psycopg.Connection, league: str) -> Index:
    events = [
        Event(str(r[0]), r[1], r[2])
        for r in conn.execute(
            "SELECT event_id, starts_at, description FROM novig_events"
            " WHERE league = %s AND starts_at IS NOT NULL",
            (league,),
        )
    ]
    markets = [
        Market(str(r[0]), str(r[1]), r[2], r[3])
        for r in conn.execute(
            "SELECT m.market_id, m.event_id, m.market_type, m.description"
            " FROM novig_markets m JOIN novig_events e USING (event_id) WHERE e.league = %s",
            (league,),
        )
    ]
    return Index.build(events, markets)


def resolve_posts(
    conn: psycopg.Connection,
    model: str,
    model_version: str,
    league: str,
    roster: Mapping[str, set[str]] | None = None,
) -> tuple[Counter[str], Counter[str]]:
    """Resolve every classified (not unknown) post. Market IDs are limited to markets in
    the trades data. Returns counts by status and unmatched subject names."""
    idx = load_index(conn, league)
    traded = {
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT market_id FROM novig_trades WHERE league = %s", (league,)
        )
    }
    rows = conn.execute(
        """
        SELECT c.post_id, p.t0, c.players, c.team
        FROM classifications c JOIN posts p USING (post_id)
        WHERE c.model = %s AND c.model_version = %s AND c.status_change <> 'unknown'
          AND c.players IS NOT NULL
        """,
        (model, model_version),
    ).fetchall()
    statuses: Counter[str] = Counter()
    unmatched: Counter[str] = Counter()
    out = []
    for post_id, t0, players, team in rows:
        for r in resolve(idx, t0, players, team, roster):
            statuses[r.status] += 1
            if r.status == "unmatched":
                unmatched[r.subject] += 1
                log.info("unmatched %r (post %s): %s", r.subject, post_id, r.reason)
            markets = [m for m in r.market_ids if m in traded]
            out.append(
                (
                    post_id,
                    model_version,
                    r.subject,
                    r.status,
                    r.reason,
                    r.event_id,
                    r.team,
                    r.tier1,
                    markets or None,
                )
            )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM entity_resolutions WHERE model_version = %s", (model_version,))
        cur.executemany(
            "INSERT INTO entity_resolutions"
            " (post_id, model_version, subject, status, reason, event_id, team, tier1,"
            " market_ids) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            out,
        )
    return statuses, unmatched


def sleeper_roster(path: Path) -> dict[str, set[str]]:
    """Normalized player name -> team nicknames from Sleeper's player list
    (data/reference/sleeper_players_nfl.json). These are current rosters, so the
    resolver uses them only to choose between the two teams a player's props allow."""
    if not path.exists():
        return {}
    out: dict[str, set[str]] = {}
    for p in json.loads(path.read_text()).values():
        team, name = p.get("team"), p.get("full_name")
        if isinstance(team, str) and isinstance(name, str) and team in NICKNAME_OF_ABBR:
            out.setdefault(normalize(name), set()).add(NICKNAME_OF_ABBR[team])
    return out
