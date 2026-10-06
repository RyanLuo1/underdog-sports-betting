"""Event clock. X post IDs are snowflakes that encode creation time (t0) in milliseconds."""

from datetime import UTC, datetime

# Milliseconds since the Unix epoch at X's snowflake epoch, 2010-11-04T01:42:54.657Z.
X_EPOCH_MS = 1288834974657
_TIMESTAMP_SHIFT = 22


def t0_ms_from_post_id(post_id: int | str) -> int:
    """Return the post's creation time as Unix epoch milliseconds."""
    pid = int(post_id)
    if pid <= 0:
        raise ValueError(f"post_id must be positive, got {post_id!r}")
    return (pid >> _TIMESTAMP_SHIFT) + X_EPOCH_MS


def t0_from_post_id(post_id: int | str) -> datetime:
    """Return the post's creation time as a UTC datetime with millisecond precision."""
    return datetime.fromtimestamp(t0_ms_from_post_id(post_id) / 1000, tz=UTC)


def now_utc() -> datetime:
    return datetime.now(tz=UTC)
