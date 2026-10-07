"""Schedule-based resolution, using only what was known at t0."""

import uuid
from datetime import UTC, datetime, timedelta

from news_edge.entities.resolver import (
    Event,
    Index,
    Market,
    created_at,
    is_game_line,
    normalize,
    prop_player,
    recent_team,
    resolve,
)

T0 = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)


def mid(created: datetime, n: int) -> str:
    """A UUIDv7-style market ID created at `created`."""
    ms = int(created.timestamp() * 1000)
    return str(uuid.UUID(int=(ms << 80) | (0x7 << 76) | n))


def game(eid: str, days: float, desc: str) -> Event:
    return Event(eid, T0 + timedelta(days=days), desc)


# Bengals: W1 vs Browns (past), W2 @ Steelers (past), W3 vs Lions (next), W4 @ Ravens.
W1 = game("w1", -17, "Cleveland Browns @ Cincinnati Bengals")
W2 = game("w2", -10, "Cincinnati Bengals @ Pittsburgh Steelers")
W3 = game("w3", 3, "Detroit Lions @ Cincinnati Bengals")
W4 = game("w4", 10, "Cincinnati Bengals @ Baltimore Ravens")
NYJ = game("nyj", -3, "New York Jets @ Buffalo Bills")
NYJ_NEXT = game("nyj2", 4, "Kansas City Chiefs @ New York Jets")
FUTURE = game("dpoy", 5, "Defensive Player Of The Year Winner")


def prop(event: Event, name: str, n: int, mtype: str = "RECEPTIONS") -> Market:
    """A prop created 2 days before its game."""
    created = mid(event.starts_at - timedelta(days=2), n)
    return Market(created, event.event_id, mtype, f"{name} 5.5 {mtype}")


CHASE_W1 = prop(W1, "Ja'Marr Chase", 1)
CHASE_W2 = prop(W2, "Ja'Marr Chase", 2)
CHASE_W3 = prop(W3, "Ja'Marr Chase", 3)  # created T0+1d: after the post
LINE_W3 = Market(mid(T0, 4), "w3", "SPREAD", "CIN -3.5")
LINE_W3_1H = Market(mid(T0, 5), "w3", "TOTAL_1H", "DET @ CIN t24.5 1H")
HALL = prop(NYJ, "Breece Hall", 6, "RUSHING_YARDS")
DPOY = Market(mid(T0 - timedelta(days=30), 7), "dpoy", "DPOTY", "Lukas Van Ness DPOTY")
IDX = Index.build(
    [W1, W2, W3, W4, NYJ, NYJ_NEXT, FUTURE],
    [CHASE_W1, CHASE_W2, CHASE_W3, LINE_W3, LINE_W3_1H, HALL, DPOY],
)


def test_helpers() -> None:
    assert is_game_line("SPREAD") and is_game_line("TOTAL_1H") and is_game_line("TEAM_TOTAL")
    assert not is_game_line("RECEPTIONS")
    assert prop_player("Mark Andrews 89.5 RECEIVING_YARDS", "RECEIVING_YARDS") == "Mark Andrews"
    assert prop_player("CIN -3.5", "SPREAD") is None
    assert normalize("Michael Pittman Jr.") == normalize("michael pittman") == "michael pittman"
    assert normalize("Ja’Marr Chase") == normalize("Ja'Marr Chase") == "jamarr chase"
    assert created_at(mid(T0, 1)) == T0
    assert "dpoy" not in IDX.events  # futures are not games


def test_team_from_prop_history_and_next_scheduled_game() -> None:
    (r,) = resolve(IDX, T0, ["Ja'Marr Chase"], None)
    # The W3 prop is created after t0: it neither chooses the game nor counts as his
    # prop at t0, so the match is team_only with the game's lines.
    assert (r.status, r.event_id, r.team, r.tier1) == ("team_only", "w3", "Bengals", True)
    assert r.market_ids == [LINE_W3.market_id, LINE_W3_1H.market_id]
    later = W3.starts_at - timedelta(days=1)  # after the W3 prop was listed
    (r,) = resolve(IDX, later, ["Ja'Marr Chase"], None)
    assert r.status == "matched"
    assert r.market_ids[0] == CHASE_W3.market_id


def test_props_listed_after_t0_do_not_count_as_history() -> None:
    early = W1.starts_at - timedelta(days=3)  # before any Chase prop existed
    (r,) = resolve(IDX, early, ["Ja'Marr Chase"], None)
    assert r.status == "unmatched" and not r.tier1
    (r,) = resolve(IDX, early, ["Ja'Marr Chase"], "Bengals")
    assert (r.status, r.event_id) == ("team_only", "w1")  # his W1 prop is not listed yet


def test_in_game_news_matches_the_game_in_progress() -> None:
    (r,) = resolve(IDX, W3.starts_at + timedelta(hours=2), ["Ja'Marr Chase"], None)
    assert r.event_id == "w3"
    (r,) = resolve(IDX, W3.starts_at + timedelta(hours=4), ["Ja'Marr Chase"], None)
    assert r.event_id == "w4"  # past the game length: post-game news is about the next


def test_one_prior_game_is_ambiguous_without_roster() -> None:
    (r,) = resolve(IDX, T0, ["Breece Hall"], None)
    assert r.status == "ambiguous" and r.tier1
    (r,) = resolve(IDX, T0, ["Breece Hall"], None, {"breece hall": {"Jets"}})
    assert (r.status, r.event_id, r.team) == ("team_only", "nyj2", "Jets")
    (r,) = resolve(IDX, T0, ["Breece Hall"], None, {"breece hall": {"Steelers"}})
    assert r.status == "ambiguous"


def test_post_team_must_agree_with_history() -> None:
    (r,) = resolve(IDX, T0, ["Ja'Marr Chase"], "Steelers")
    assert r.status == "ambiguous"
    (r,) = resolve(IDX, T0, ["Backup Lineman"], "Bengals")
    assert (r.status, r.event_id, r.tier1) == ("team_only", "w3", False)
    assert r.market_ids == [LINE_W3.market_id, LINE_W3_1H.market_id]
    (r,) = resolve(IDX, T0, ["Backup Lineman"], None)
    assert r.status == "unmatched" and "no props before t0" in (r.reason or "")


def test_recent_team_follows_a_trade() -> None:
    a = game("a", -30, "Cleveland Browns @ Cincinnati Bengals")  # old team: Browns
    b = game("b", -23, "Cleveland Browns @ Pittsburgh Steelers")
    c = game("c", -9, "Baltimore Ravens @ New York Jets")  # traded to the Jets
    d = game("d", -2, "New York Jets @ Buffalo Bills")
    assert recent_team([a, b]) == {"Browns"}
    assert recent_team([a, b, c, d]) == {"Jets"}
    assert recent_team([a, b, c]) == {"Ravens", "Jets"}  # one game since the trade
