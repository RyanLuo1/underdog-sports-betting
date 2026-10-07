"""Novig events and markets from the history API, for names behind trade market IDs.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE novig_events (
            event_id    uuid        PRIMARY KEY,
            sport       text,
            league      text,
            status      text        NOT NULL,
            description text        NOT NULL,
            starts_at   timestamptz,
            synced_at   timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX novig_events_league_start ON novig_events (league, starts_at)")
    op.execute("""
        CREATE TABLE novig_markets (
            market_id   uuid        PRIMARY KEY,
            event_id    uuid        NOT NULL,
            market_type text        NOT NULL,
            strike      text,
            status      text        NOT NULL,
            description text        NOT NULL,
            outcomes    jsonb       NOT NULL,
            synced_at   timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX novig_markets_event ON novig_markets (event_id)")


def downgrade() -> None:
    op.execute("DROP TABLE novig_markets")
    op.execute("DROP TABLE novig_events")
