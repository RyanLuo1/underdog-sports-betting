"""Write classifier results to the classifications table."""

import psycopg
from psycopg.types.json import Jsonb

from news_edge.classify import rules


def classify_posts(conn: psycopg.Connection, account: str, sport: str) -> dict[str, int]:
    """Classify every post from `account` with the current rules version (replacing any
    earlier result for that version). Returns counts by status."""
    posts = conn.execute(
        "SELECT post_id, text FROM posts WHERE lower(account) = lower(%s)", (account,)
    ).fetchall()
    counts: dict[str, int] = {}
    rows = []
    for post_id, text in posts:
        c = rules.classify(text)
        counts[c.status] = counts.get(c.status, 0) + 1
        rows.append(
            (
                post_id,
                rules.MODEL,
                rules.VERSION,
                sport if c.status != "unknown" else None,
                c.team,
                c.players or None,
                c.status,
                c.template,
                Jsonb(c.detail) if c.status != "unknown" else None,
            )
        )
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO classifications (post_id, model, model_version, sport, team, players,
                                         status_change, template, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (post_id, model, model_version) DO UPDATE SET
                sport = EXCLUDED.sport, team = EXCLUDED.team, players = EXCLUDED.players,
                status_change = EXCLUDED.status_change, template = EXCLUDED.template,
                detail = EXCLUDED.detail, classified_at = now()
            """,
            rows,
        )
    return counts
