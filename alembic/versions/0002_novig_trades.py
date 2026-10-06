"""Novig public trades and the file log for the daily backfill.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One row per side of a trade. A trade is the group (ts, market_id); taker_count > 1
    # means makers cannot be paired to takers, so the group is flagged ambiguous.
    # source is 'public_csv' for Novig's daily files; our own fills will use 'own_fill'.
    op.execute("""
        CREATE TABLE novig_trades (
            ts              timestamptz      NOT NULL,
            source          text             NOT NULL DEFAULT 'public_csv',
            file_date       date,
            row_num         integer,
            outcome_id      uuid             NOT NULL,
            market_id       uuid             NOT NULL,
            contract_series text             NOT NULL,
            league          text,
            market_type     text,
            trade_type      text             NOT NULL,
            legs            smallint         NOT NULL,
            cost            double precision NOT NULL,
            qty             double precision NOT NULL,
            price           double precision NOT NULL,
            side            text             NOT NULL CHECK (side IN ('TAKER', 'MAKER')),
            taker_count     smallint         NOT NULL,
            ambiguous       boolean          NOT NULL
        )
    """)
    op.execute("SELECT create_hypertable('novig_trades', by_range('ts', INTERVAL '1 day'))")
    op.execute("CREATE INDEX novig_trades_market_ts ON novig_trades (market_id, ts)")
    op.execute("CREATE INDEX novig_trades_file ON novig_trades (file_date, source)")

    op.execute("""
        CREATE TABLE novig_trade_files (
            file_date     date        PRIMARY KEY,
            etag          text,
            size_bytes    bigint      NOT NULL,
            row_count     integer,
            downloaded_at timestamptz NOT NULL,
            loaded_at     timestamptz
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE novig_trade_files")
    op.execute("DROP TABLE novig_trades")
