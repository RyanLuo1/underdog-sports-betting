"""Resolution of classified posts to Novig events and markets.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One row per (post, classifier version, subject). status is matched, team_only
    # (the player's team's next game, without props for the player), ambiguous, or
    # unmatched. Only matched and team_only rows carry an event and markets. tier1: Novig
    # listed props for the player in some game before the post.
    op.execute("""
        CREATE TABLE entity_resolutions (
            post_id       bigint      NOT NULL REFERENCES posts (post_id),
            model_version text        NOT NULL,
            subject       text        NOT NULL,
            status        text        NOT NULL,
            reason        text,
            event_id      uuid,
            team          text,
            tier1         boolean     NOT NULL DEFAULT false,
            market_ids    uuid[],
            resolved_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (post_id, model_version, subject)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE entity_resolutions")
