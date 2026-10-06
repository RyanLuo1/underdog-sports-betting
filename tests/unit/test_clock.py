from datetime import UTC, datetime

import pytest

from news_edge.core.clock import X_EPOCH_MS, t0_from_post_id, t0_ms_from_post_id


def make_post_id(epoch_ms: int, low_bits: int = 0) -> int:
    return ((epoch_ms - X_EPOCH_MS) << 22) | low_bits


def test_snowflake_epoch() -> None:
    assert t0_from_post_id(1) == datetime(2010, 11, 4, 1, 42, 54, 657000, tzinfo=UTC)


def test_round_trip_keeps_milliseconds() -> None:
    t = datetime(2026, 10, 20, 23, 30, 1, 234000, tzinfo=UTC)
    ms = int(t.timestamp() * 1000)
    post_id = make_post_id(ms, low_bits=(1 << 22) - 1)
    assert t0_ms_from_post_id(post_id) == ms
    assert t0_ms_from_post_id(str(post_id)) == ms
    assert t0_from_post_id(post_id) == t


@pytest.mark.parametrize("bad", [0, -5, "0"])
def test_rejects_non_positive(bad: int | str) -> None:
    with pytest.raises(ValueError):
        t0_ms_from_post_id(bad)
