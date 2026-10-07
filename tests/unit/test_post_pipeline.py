"""Classify and resolve stored posts end to end on the test database."""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from news_edge.classify import rules
from news_edge.classify.store import classify_posts
from news_edge.core.clock import X_EPOCH_MS
from news_edge.entities.store import resolve_posts

T0 = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)
EVENT = "019fcbf1-d3f9-7312-832f-329281b1dd1b"
PROP = "019fcbf1-d4f0-7a43-afdf-59c087a839e1"
UNTRADED = "019fcbf1-d4f0-7a43-afdf-59c087a839e2"
LINE = "019fcbf1-d4f0-7a43-afdf-59c087a839e3"
OUTCOME = "019fcbf1-d4f0-7a43-afdf-59c087a839f0"


def _post(conn: psycopg.Connection, n: int, text: str) -> int:
    pid = ((int(T0.timestamp() * 1000) - X_EPOCH_MS) << 22) | n
    conn.execute(
        "INSERT INTO posts (post_id, account, text, t0, is_retweet, is_quote, raw,"
        " scraped_via, fetched_at) VALUES (%s, 'UnderdogNFL', %s, %s, false, false, %s,"
        " 'test', now())",
        (pid, text, T0, Jsonb({})),
    )
    return pid


@pytest.mark.db
def test_classify_and_resolve(db: psycopg.Connection) -> None:
    out = _post(db, 1, "Ja'Marr Chase (knee) ruled out for Week 2.")
    unknown = _post(db, 2, "Schefter: Ja'Marr Chase could play Week 2.")
    nobody = _post(db, 3, "Some Backup (ankle) ruled out for Week 2.")

    db.execute(
        "INSERT INTO novig_events (event_id, league, status, description, starts_at)"
        " VALUES (%s, 'NFL', 'FINAL', 'Detroit Lions @ Cincinnati Bengals', %s)",
        (EVENT, T0 + timedelta(days=3)),
    )
    for market, mtype, desc in [
        (PROP, "RECEPTIONS", "Ja'Marr Chase 6.5 RECEPTIONS"),
        (UNTRADED, "RECEIVING_YARDS", "Ja'Marr Chase 80.5 RECEIVING_YARDS"),
        (LINE, "SPREAD", "CIN -3.5"),
    ]:
        db.execute(
            "INSERT INTO novig_markets (market_id, event_id, market_type, status, description,"
            " outcomes) VALUES (%s, %s, %s, 'SETTLED', %s, '[]')",
            (market, EVENT, mtype, desc),
        )
    for market in (PROP, LINE):
        db.execute(
            "INSERT INTO novig_trades (ts, file_date, row_num, outcome_id, market_id,"
            " contract_series, league, market_type, trade_type, legs, cost, qty, price, side,"
            " taker_count, ambiguous) VALUES (%s, '2026-09-10', 1, %s, %s, 'x', 'NFL', 'x',"
            " 'STRAIGHT', 1, 50, 100, 0.5, 'TAKER', 1, false)",
            (T0, OUTCOME, market),
        )

    counts = classify_posts(db, "UnderdogNFL", "NFL")
    assert counts == {"out": 2, "unknown": 1}
    status: dict[int, str] = dict(
        db.execute(
            "SELECT post_id, status_change FROM classifications WHERE model_version = %s",
            (rules.VERSION,),
        ).fetchall()
    )
    assert status == {out: "out", unknown: "unknown", nobody: "out"}

    # Chase's only prop history is one Lions-Bengals game: ambiguous on its own.
    statuses, _ = resolve_posts(db, rules.MODEL, rules.VERSION, "NFL")
    assert statuses == {"ambiguous": 1, "unmatched": 1}
    # The roster breaks the tie between the game's two teams.
    roster = {"jamarr chase": {"Bengals"}}
    statuses, unmatched = resolve_posts(db, rules.MODEL, rules.VERSION, "NFL", roster)
    assert statuses == {"matched": 1, "unmatched": 1}
    assert unmatched == {"Some Backup": 1}
    row = db.execute(
        "SELECT status, event_id::text, team, tier1, market_ids::text[] FROM entity_resolutions"
        " WHERE post_id = %s",
        (out,),
    ).fetchone()
    # The untraded prop is dropped: market IDs come from the trades data.
    assert row == ("matched", EVENT, "Bengals", True, [PROP, LINE])
