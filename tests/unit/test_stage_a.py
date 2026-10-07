"""Stage A: side rules, timing, both stale views, controls, and the expanding fit."""

import uuid
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from news_edge.entities.resolver import Event, Index, Market
from news_edge.research import stage_a as sa

T0 = datetime(2026, 9, 27, 15, 30, tzinfo=UTC)  # Week 3, pre-game
KICK = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)
UNDER = sa.Outcome("o-under", "Under 89.5")
OVER = sa.Outcome("o-over", "Over 89.5")
PROP = sa.MarketInfo(
    "m-prop", "RECEIVING_YARDS", "Ja'Marr Chase 89.5 RECEIVING_YARDS", (OVER, UNDER)
)
TEE = sa.MarketInfo("m-tee", "RECEIVING_YARDS", "Tee Higgins 59.5 RECEIVING_YARDS", (OVER, UNDER))
DET, CIN = sa.Outcome("o-det", "DET +3.5"), sa.Outcome("o-cin", "CIN -3.5")
SPREAD = sa.MarketInfo("m-spread", "SPREAD", "CIN -3.5", (CIN, DET))
ML_1H = sa.MarketInfo(
    "m-ml1h", "MONEY_1H", "CIN 1H", (sa.Outcome("a", "CIN"), sa.Outcome("b", "DET"))
)
TOTAL = sa.MarketInfo("m-total", "TOTAL", "DET @ CIN t47.5", (OVER, UNDER))
TT = sa.MarketInfo("m-tt", "TEAM_TOTAL", "CIN 24.5 TEAM_TOTAL", (OVER, UNDER))
OPP_TT = sa.MarketInfo("m-ott", "TEAM_TOTAL", "DET 23.5 TEAM_TOTAL", (OVER, UNDER))
P = sa.Params()

Spec = tuple[float, str, str, float, float, bool]


def news(status: str = "doubtful", t0: datetime = T0, team: str | None = "Bengals") -> sa.NewsEvent:
    return sa.NewsEvent(
        post_id=1,
        t0=t0,
        status=status,
        subject="Ja'Marr Chase",
        event_id="e1",
        game_start=KICK,
        game_teams=("Lions", "Bengals"),
        team=team,
        tier1=True,
        markets=(PROP, TEE, SPREAD, ML_1H, TOTAL, TT, OPP_TT),
    )


# Sides


def test_sides_for_bad_news() -> None:
    n = news()
    sides = {m.market_id: sa.favored_side(n, m) for m in n.markets}
    assert sides["m-prop"].favored == UNDER
    assert sides["m-tee"].favored is None
    assert sides["m-spread"].favored == DET
    ml = sides["m-ml1h"].favored
    assert ml is not None and ml.name == "DET"
    assert sides["m-total"].favored is None
    assert sides["m-tt"].favored == UNDER
    assert sides["m-ott"].favored is None


def test_pregame_out_excludes_own_props_in_game_keeps_them() -> None:
    assert sa.favored_side(news("inactive"), PROP).favored is None
    in_game = news("in_game_injury", KICK + timedelta(minutes=40))
    assert sa.favored_side(in_game, PROP).favored == UNDER
    assert sa.favored_side(news(team=None), SPREAD).favored is None


# Measuring

SIDE = sa.Side(PROP, "player_prop", UNDER, "under")


def rows(*spec: Spec) -> pl.DataFrame:
    """(seconds from T0, outcome, side, price, qty, ambiguous)"""
    return pl.DataFrame(
        [
            {
                "ts": T0 + timedelta(seconds=s),
                "market_id": "m-prop",
                "outcome_id": o,
                "side": side,
                "price": p,
                "qty": q,
                "cost": p * q,
                "ambiguous": a,
            }
            for s, o, side, p, q, a in spec
        ],
        schema=sa.TRADE_SCHEMA,
    )


def taker(s: float, p: float, q: float, amb: bool = False, o: str = "o-under") -> Spec:
    return (s, o, "TAKER", p, q, amb)


def maker(s: float, p_fav: float, q: float) -> Spec:
    """A maker on the other side at this level: maker price = 1 - favored price."""
    return (s, "o-over", "MAKER", round(1 - p_fav, 6), q, False)


def test_pre_price_uses_favored_takers_last_millisecond_strictly_before_t0() -> None:
    t = rows(
        taker(-900, 0.40, 10),
        taker(-60, 0.44, 10),
        taker(-60, 0.48, 30),  # same ms: VWAP 0.47
        taker(-30, 0.30, 10, o="o-over"),  # other side: never the pre-news price
        taker(0, 0.90, 10),  # at t0: not before
    )
    r = sa.measure(t, SIDE, T0, P)
    assert r is not None
    assert r["pre_price"] == pytest.approx(0.47)
    assert r["pre_age_s"] == 60
    assert r["excluded"] is None


def test_old_or_missing_pre_trade_is_excluded() -> None:
    r = sa.measure(rows(taker(-1900, 0.45, 10)), SIDE, T0, P)
    assert r is not None and r["excluded"] == "pre-news trade older than max_pre_age"
    assert "xp_fills" not in r
    r = sa.measure(rows(taker(-3700, 0.45, 10)), SIDE, T0, P)
    assert r is not None and r["excluded"] == "no favored-side trade in lookback"


def test_drift_ex_post_view_levels_ambiguous_and_buckets() -> None:
    t = rows(
        taker(-900, 0.40, 10),  # lead price: last trade at or before t0 - 10 min
        taker(-30, 0.45, 10),  # pre-news 0.45, drift +0.05
        # post-news 0.65 -> stale if below the midpoint 0.55
        taker(5, 0.47, 20), maker(5, 0.47, 20),  # stale, 0-10 s
        taker(20, 0.53, 30), maker(20, 0.46, 10), maker(20, 0.565, 20),  # sweep: 10 stale
        taker(40, 0.50, 25, amb=True),  # stale, ambiguous: timing and upper bound only
        taker(120, 0.60, 40), maker(120, 0.60, 40),  # repriced: not stale
        taker(500, 0.50, 5), maker(500, 0.50, 5),  # stale, 5-10 min
        taker(601, 0.40, 99), maker(601, 0.40, 99),  # after the window
        taker(1800, 0.65, 5),  # post-news price (ex post)
    )  # fmt: skip
    r = sa.measure(t, SIDE, T0, P)
    assert r is not None
    assert r["pre_price"] == pytest.approx(0.45)
    assert r["lead_price"] == pytest.approx(0.40)
    assert r["drift"] == pytest.approx(0.05)
    assert r["post_price"] == pytest.approx(0.65)
    assert r["move"] == pytest.approx(0.20)
    assert r["moved"] is True
    assert r["xp_fills"] == 4  # three clean levels and one ambiguous group
    assert (r["xp_first_s"], r["xp_last_s"]) == (5, 500)
    assert r["xp_qty"] == pytest.approx(20 + 10 + 5)
    assert r["xp_ambiguous_qty"] == pytest.approx(25)
    assert r["xp_qty_upper"] == pytest.approx(60)
    assert (r["xp_qty_0_10s"], r["xp_qty_10_30s"], r["xp_qty_5_10m"]) == (20, 10, 5)
    assert r["xp_qty_30_60s"] == 0  # the ambiguous fill is not in the clean buckets
    assert "t0_fills" not in r  # no expected move given


def test_t0_view_uses_expected_move_not_post_price() -> None:
    t = rows(taker(-30, 0.45, 10), taker(5, 0.47, 20), maker(5, 0.47, 20), taker(1800, 0.46, 1))
    r = sa.measure(t, SIDE, T0, P, expected_move=0.10)  # threshold 0.50
    assert r is not None
    assert r["moved"] is False  # ex post the price barely moved
    assert (r["t0_fills"], r["t0_qty"]) == (1, 20)
    r = sa.measure(t, SIDE, T0, P, expected_move=0.03)  # threshold 0.465
    assert r is not None and r["t0_fills"] == 0
    r = sa.measure(t, SIDE, T0, P, expected_move=-0.01)
    assert r is not None and "t0_fills" not in r


def test_no_side_no_measurement() -> None:
    assert sa.measure(rows(), sa.Side(TOTAL, "game_line", None, "x"), T0, P) is None


# Weeks, fit, controls


def test_nfl_week() -> None:
    assert sa.nfl_week(datetime(2026, 9, 10, 0, 20, tzinfo=UTC), P) == 1  # Wed 8:20 PM ET
    assert sa.nfl_week(datetime(2026, 9, 15, 0, 15, tzinfo=UTC), P) == 1  # Mon night ET
    assert sa.nfl_week(datetime(2026, 9, 18, 0, 15, tzinfo=UTC), P) == 2
    assert sa.nfl_week(datetime(2026, 8, 30, 20, 0, tzinfo=UTC), P) is None


def fit_row(post: int, days: float, move: float = 0.1, week: int | None = 2) -> dict[str, object]:
    return {
        "post_id": post,
        "moment": T0 + timedelta(days=days),
        "status": "out",
        "kind": "game_line",
        "tier1": True,
        "week": week,
        "move": move,
    }


def test_fit_expanding_window_minimum_and_fallback() -> None:
    fit = sa.Fit()
    for i in range(5):
        fit.add(fit_row(i, -10 + i, move=0.10 + i / 100), P)
    fit.add(fit_row(99, -20, move=5.0, week=None), P)  # preseason: never fitted
    fit.add(fit_row(98, -0.01, move=5.0), P)  # settles after T0: not yet usable
    exp, level = fit.expected(T0, "out", "game_line", True, 5)
    assert level == "status_kind_tier" and exp == pytest.approx(0.12)
    exp, level = fit.expected(T0, "out", "player_prop", True, 5)
    assert level == "status" and exp == pytest.approx(0.12)
    assert fit.expected(T0, "doubtful", "game_line", True, 5) == (None, "too few earlier events")
    assert fit.expected(T0 - timedelta(days=7), "out", "game_line", True, 5)[0] is None


def _mid(created: datetime, n: int) -> str:
    return str(uuid.UUID(int=(int(created.timestamp() * 1000) << 80) | (0x7 << 76) | n))


def test_placebo_moments_same_offset_other_weeks_and_gap() -> None:
    desc = "Detroit Lions @ Cincinnati Bengals"
    games = [Event(f"g{w}", KICK + timedelta(days=7 * (w - 3)), desc) for w in (1, 2, 3, 4)]
    pre = Event("pre", datetime(2026, 8, 20, 23, tzinfo=UTC), desc)
    line = {
        g.event_id: Market(_mid(g.starts_at, i), g.event_id, "SPREAD", "CIN -3.5")
        for i, g in enumerate(games)
    }
    idx = Index.build([*games, pre], list(line.values()))
    markets = {
        m.market_id: sa.MarketInfo(m.market_id, "SPREAD", "CIN -3.5", (CIN, DET))
        for m in line.values()
    }
    base = news()
    n = sa.NewsEvent(
        base.post_id, base.t0, base.status, base.subject, "g3", KICK, base.game_teams,
        base.team, base.tier1, base.markets,
    )  # fmt: skip
    offset = KICK - T0
    # A Lions post 30 minutes after week 4's moment rules week 4 out.
    by_team = {"Lions": [games[3].starts_at - offset + timedelta(minutes=30)]}
    # A Chiefs post near week 2's moment is about another game: no effect.
    by_team["Chiefs"] = [games[1].starts_at - offset]
    got, short = sa.placebo_moments(n, idx, markets, by_team, [], P)
    assert [m.event_id for m in got] == ["g2", "g1"]  # nearest weeks; never preseason
    assert all(m.game_start - m.at == offset for m in got)
    assert short == "2 of 3 placebo games"
    assert got[0].markets[0].market_id == line["g2"].market_id
    # The placebo game's own teams decide the opponent: a side is found on its lines.
    assert got[0].game_teams == ("Lions", "Bengals")
    assert sa.favored_side(got[0].as_news(), got[0].markets[0]).favored == DET
    assert sa.placebo_moments(news(team=None), idx, markets, {}, [], P) == ([], "team unknown")
    # A post about the subject himself near week 2's moment rules week 2 out.
    got, _ = sa.placebo_moments(n, idx, markets, {}, [games[1].starts_at - offset], P)
    assert [m.event_id for m in got] == ["g4", "g1"]
    # Only games the trade data covers, and only traded markets.
    data_end = games[1].starts_at - offset + timedelta(minutes=30)  # mid week 2's settle
    got, _ = sa.placebo_moments(n, idx, markets, {}, [], P, data_end=data_end)
    assert [m.event_id for m in got] == ["g1"]
    got, _ = sa.placebo_moments(n, idx, markets, {}, [], P, traded=set())
    assert all(m.markets == () for m in got)


def test_params_defaults() -> None:
    assert P.regular_season_start == date(2026, 9, 8)
    assert P.control_gap >= max(P.lookback, P.settle)


# Second-review fixes


def test_fill_at_exactly_t0_counts_and_drift_needs_a_recent_trade() -> None:
    t = rows(
        taker(-900, 0.40, 10),  # the only trade before t0: older than the drift lead
        taker(0, 0.41, 7), maker(0, 0.41, 7),  # exactly at t0: a fill, not pre-news
        taker(1800, 0.60, 1),
    )  # fmt: skip
    r = sa.measure(t, SIDE, T0, P)
    assert r is not None
    assert r["recent_pre_trade"] is False  # pre-news trade is the lead trade itself
    assert r["xp_fills"] == 1 and r["xp_first_s"] == 0


def test_data_end_and_game_end_exclusions() -> None:
    t = rows(taker(-30, 0.45, 10), taker(5, 0.46, 5), maker(5, 0.46, 5), taker(1800, 0.70, 1))
    r = sa.measure(t, SIDE, T0, P, data_end=T0 + timedelta(minutes=59))
    assert r is not None and r["excluded"] == "trade data ends inside the settle window"
    # In-game, 3 h 25 min after kickoff: the game ends 5 minutes in, before the window.
    kick = T0 - timedelta(hours=3, minutes=25)
    r = sa.measure(t, SIDE, T0, P, game_start=kick)
    assert r is not None and r["excluded"] == "game ends before the post-news window"
    # 3 h 5 min after kickoff: the post-news window is cut at kickoff + 3.5 h = t0 + 25 min.
    kick = T0 - timedelta(hours=3, minutes=5)
    r = sa.measure(t, SIDE, T0, P, game_start=kick)
    assert r is not None and r["excluded"] is None and r["post_cut_short"] is True
    assert r["post_price"] is None  # the 30-minute trade is after the cut
    r = sa.measure(t, SIDE, T0, P, game_start=KICK)  # pre-game: full window
    assert r is not None and r["post_cut_short"] is False and r["post_price"] == 0.70


def test_fit_skips_cut_short_and_pools_for_controls() -> None:
    fit = sa.Fit()
    for i in range(5):
        fit.add(fit_row(i, -10 + i, move=0.10), P)
    cut = fit_row(50, -5, move=9.0) | {"post_cut_short": True}
    fit.add(cut, P)
    excluded = fit_row(51, -5, move=9.0) | {"excluded": "x"}
    fit.add(excluded, P)
    assert len(fit.rows) == 5
    exp, level = fit.expected(T0, None, "game_line", True, 5)
    assert level == "pooled_kind_tier" and exp == pytest.approx(0.10)
    exp, level = fit.expected(T0, None, "player_prop", True, 5)
    assert level == "pooled" and exp == pytest.approx(0.10)


def test_drop_overlapping_news() -> None:
    def ev(post: int, minutes: float, event: str = "e1") -> sa.NewsEvent:
        n = news()
        return sa.NewsEvent(
            post, T0 + timedelta(minutes=minutes), n.status, f"P{post}", event, KICK,
            n.game_teams, n.team, n.tier1, (),
        )  # fmt: skip

    a, b, c, d, e = ev(1, 0), ev(2, 3), ev(3, 70), ev(4, 75, "e2"), ev(5, 3)
    kept, dropped = sa.drop_overlapping([a, b, c, d, e], P)
    # b and e follow a on the same game within an hour; c is 70 minutes later; d is
    # another game.
    assert [n.post_id for n in kept] == [1, 3, 4]
    assert [n.post_id for n in dropped] == [2, 5]


def test_placebo_uses_its_own_opponent() -> None:
    other = Event("g9", KICK + timedelta(days=7), "Cincinnati Bengals @ Baltimore Ravens")
    line = Market(_mid(other.starts_at, 9), "g9", "SPREAD", "BAL -2.5")
    idx = Index.build([other], [line])
    bal = sa.MarketInfo(
        line.market_id, "SPREAD", "BAL -2.5",
        (sa.Outcome("bal", "BAL -2.5"), sa.Outcome("cin", "CIN +2.5")),
    )  # fmt: skip
    got, _ = sa.placebo_moments(news(), idx, {line.market_id: bal}, {}, [], P)
    (m,) = got
    side = sa.favored_side(m.as_news(), m.markets[0])
    assert side.favored is not None and side.favored.name == "BAL -2.5"
