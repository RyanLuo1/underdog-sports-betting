"""Posts from news accounts, and their classifications.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # t0 is decoded from post_id (core/clock.py); created_at is the source's own,
    # second-precision timestamp, kept to cross-check t0.
    op.execute("""
        CREATE TABLE posts (
            post_id     bigint      PRIMARY KEY,
            account     text        NOT NULL,
            text        text        NOT NULL,
            t0          timestamptz NOT NULL,
            created_at  timestamptz,
            is_retweet  boolean     NOT NULL,
            is_quote    boolean     NOT NULL,
            raw         jsonb       NOT NULL,
            scraped_via text        NOT NULL,
            fetched_at  timestamptz NOT NULL,
            deleted_at  timestamptz
        )
    """)
    op.execute("CREATE INDEX posts_account_t0 ON posts (account, t0)")

    # One row per post per classifier version. model and model_version name the
    # classifier, so a different one could be compared later.
    op.execute("""
        CREATE TABLE classifications (
            post_id       bigint      NOT NULL REFERENCES posts (post_id),
            model         text        NOT NULL,
            model_version text        NOT NULL,
            sport         text,
            team          text,
            players       text[],
            status_change text        NOT NULL,
            template      text,
            detail        jsonb,
            classified_at timestamptz NOT NULL DEFAULT now(),
            reviewed_label text,
            PRIMARY KEY (post_id, model, model_version)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE classifications")
    op.execute("DROP TABLE posts")
