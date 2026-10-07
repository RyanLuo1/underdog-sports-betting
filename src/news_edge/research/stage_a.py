"""Stage A: do Novig prices stay stale after NFL injury news? Trades only, NFL only.

For each material news event (a classified, resolved post) and each affected market with
a side the news favors, look at TAKER trades that bought the favored outcome in the
`window` after t0, and ask which of them were still at stale prices.

Two views, reported side by side:

- **After-the-fact (ex post).** The post-news price is the favored side's price 10 to
  60 minutes after t0. A market counts as moved if that price rose by at least
  `min_move`. A fill is stale if its price is closer to the pre-news price than to the
  post-news price. This uses prices stamped after t0: it measures what happened, is not
  information anyone had at t0, and must never feed a signal.
- **At t0 (point in time).** No price-based filter. The expected move for the event's
  type is the median realized move of earlier regular-season news markets whose settle
  window closed before this t0 (expanding window; at least `fit_min_events` events,
  falling back from (status, kind, tier) to status). A fill is stale if it is closer to
  the pre-news price than to pre-news + expected move.

Prices:
- Every price is the favored side's ask: what a taker paid for the favored outcome. The
  pre-news price is the VWAP of the last millisecond of favored-side TAKER trades
  strictly before t0, within `lookback`. Markets whose pre-news trade is older than
  `max_pre_age` are excluded. Drift compares it with the same price at t0 - `drift_lead`.
- A TAKER row's price is the VWAP of a sweep across makers. For unambiguous groups the
  sweep is split into maker levels (favored price = 1 - maker price), so only stale
  levels count. Ambiguous groups (several takers in one millisecond and market) cannot be
  split: their size is reported separately, and an upper bound includes them.

Markets and sides are chosen from the news and market names only, never from prices.
"""

import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median
from typing import Literal
from zoneinfo import ZoneInfo

import polars as pl
import psycopg

from news_edge.entities.resolver import GAME_LENGTH, Index, normalize, prop_player
from news_edge.entities.teams import ABBRS, game_teams

MATERIAL = ("out", "doubtful", "inactive", "ir", "in_game_injury")
# Good-news statuses, measured with the same rules as a control.
CONTROL = ("probable", "active", "practice_full")
# Pre-game, the player will not play, so his own props are expected to void (Novig
# settles FMV markets at fair value, not a refund). Excluded before looking at prices.
PROPS_EXPECTED_TO_VOID = frozenset({"out", "inactive", "ir"})

ET = ZoneInfo("America/New_York")
GAME_SIDE_TYPES = frozenset({"MONEY", "SPREAD"})
BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0_10s", 0, 10),
    ("10_30s", 10, 30),
    ("30_60s", 30, 60),
    ("1_5m", 60, 300),
    ("5_10m", 300, 600.001),
)
Kind = Literal["player_prop", "game_line", "team_total"]


@dataclass(frozen=True)
class Params:
    window: timedelta = timedelta(minutes=10)
    lookback: timedelta = timedelta(minutes=60)
    settle: timedelta = timedelta(minutes=60)
    max_pre_age: timedelta = timedelta(minutes=30)
    drift_lead: timedelta = timedelta(minutes=10)
    min_move: float = 0.03  # placeholder: smallest favored-side rise that counts as moved
    fit_min_events: int = 5
    control_gap: timedelta = timedelta(minutes=60)  # >= lookback and settle
    placebo_games: int = 3
    regular_season_start: date = date(2026, 9, 8)  # Tuesday of NFL Week 1 (ET)
    # In-game, the post-news window ends at kickoff + game_length (placeholder), so the
    # post-news price is not the final-result price.
    game_length: timedelta = GAME_LENGTH


def nfl_week(kickoff: datetime, params: Params) -> int | None:
    """NFL week (Tuesday to Monday, ET) of a kickoff, or None for preseason."""
    days = (kickoff.astimezone(ET).date() - params.regular_season_start).days
    return None if days < 0 else days // 7 + 1


@dataclass(frozen=True)
class Outcome:
    outcome_id: str
    name: str


@dataclass(frozen=True)
class MarketInfo:
    market_id: str
    market_type: str
    description: str
    outcomes: tuple[Outcome, ...]

    @property
    def base_type(self) -> str:
        for suffix in ("_1H", "_2H", "_1Q", "_2Q", "_3Q", "_4Q"):
            if self.market_type.endswith(suffix):
                return self.market_type.removesuffix(suffix)
        return self.market_type


@dataclass(frozen=True)
class NewsEvent:
    """One subject (player) of one classified, resolved post."""

    post_id: int
    t0: datetime
    status: str
    subject: str
    event_id: str
    game_start: datetime
    game_teams: tuple[str, str]
    team: str | None  # the subject's team, from the resolver
    tier1: bool  # Novig listed props for him in some game before t0
    markets: tuple[MarketInfo, ...]


@dataclass(frozen=True)
class Side:
    market: MarketInfo
    kind: Kind
    favored: Outcome | None
    reason: str


def _starts_with_team(name: str, nickname: str) -> bool:
    first = name.split()[0] if name.split() else ""
    return first in ABBRS[nickname] or nickname in name


def favored_side(news: NewsEvent, market: MarketInfo) -> Side:
    """The outcome bad news for `news.subject` favors, or None with the reason why.
    Uses only the news and the market's names, never prices."""
    pregame = news.t0 < news.game_start
    names = [o.name for o in market.outcomes]
    prop_of = prop_player(market.description, market.market_type)
    if prop_of is not None and normalize(prop_of) != normalize(news.subject):
        return Side(market, "player_prop", None, "another player's prop")
    if prop_of is not None:
        if pregame and news.status in PROPS_EXPECTED_TO_VOID:
            return Side(market, "player_prop", None, "player out pre-game; prop expected to void")
        if len(market.outcomes) != 2:
            return Side(market, "player_prop", None, f"{len(market.outcomes)} outcomes")
        for o in market.outcomes:
            if o.name.startswith("Under") or o.name == "No":
                return Side(market, "player_prop", o, "under / no")
        return Side(market, "player_prop", None, f"no under/no outcome in {names}")
    if news.team is None:
        return Side(market, "game_line", None, "subject's team unknown")
    opponent = next(t for t in news.game_teams if t != news.team)
    if market.base_type in GAME_SIDE_TYPES:
        if len(market.outcomes) != 2:
            return Side(market, "game_line", None, f"{len(market.outcomes)} outcomes")
        hits = [o for o in market.outcomes if _starts_with_team(o.name, opponent)]
        if len(hits) == 1:
            return Side(market, "game_line", hits[0], f"opponent {opponent}")
        return Side(market, "game_line", None, f"cannot find {opponent} in {names}")
    if market.base_type == "TEAM_TOTAL":
        if not _starts_with_team(market.description, news.team):
            return Side(market, "team_total", None, "opponent's team total")
        unders = [o for o in market.outcomes if o.name.startswith("Under")]
        if len(unders) == 1:
            return Side(market, "team_total", unders[0], "subject's team total under")
        return Side(market, "team_total", None, f"no under in {names}")
    return Side(market, "game_line", None, f"no clear side for {market.market_type}")


# Measuring one market at one moment

LEVEL_SCHEMA = pl.Schema(
    {
        "ts": pl.Datetime("us", "UTC"),
        "price": pl.Float64(),
        "qty": pl.Float64(),
        "ambiguous": pl.Boolean(),
    }
)


def last_price(
    takers: pl.DataFrame, since: datetime, before: datetime, inclusive_end: bool = False
) -> tuple[float, datetime] | None:
    """VWAP of the last millisecond of `takers` in [since, before) (or [since, before])."""
    end = pl.col("ts") <= before if inclusive_end else pl.col("ts") < before
    rows = takers.filter((pl.col("ts") >= since) & end)
    if rows.is_empty():
        return None
    last: datetime = rows["ts"].max()  # type: ignore[assignment]
    group = rows.filter(pl.col("ts") == last)
    return float(group["cost"].sum()) / float(group["qty"].sum()), last


def fills(trades: pl.DataFrame, fav: str, start: datetime, end: datetime) -> pl.DataFrame:
    """Favored-side fills in [start, end] as levels (ts, price, qty, ambiguous).
    Unambiguous sweeps split into maker levels (favored price = 1 - maker price);
    ambiguous groups stay whole at the TAKER row's VWAP."""
    window = trades.filter((pl.col("ts") >= start) & (pl.col("ts") <= end))
    takers = window.filter((pl.col("side") == "TAKER") & (pl.col("outcome_id") == fav))
    if takers.is_empty():
        return pl.DataFrame(schema=LEVEL_SCHEMA)
    clean_ts = takers.filter(~pl.col("ambiguous"))["ts"].implode()
    makers = window.filter(
        (pl.col("side") == "MAKER") & (pl.col("outcome_id") != fav) & pl.col("ts").is_in(clean_ts)
    ).select("ts", (1.0 - pl.col("price")).alias("price"), "qty", pl.lit(False).alias("ambiguous"))
    whole = takers.filter(pl.col("ambiguous")).select("ts", "price", "qty", "ambiguous")
    return pl.concat([makers.cast(LEVEL_SCHEMA), whole.cast(LEVEL_SCHEMA)]).sort("ts")


def stale_stats(
    levels: pl.DataFrame, threshold: float, at: datetime, prefix: str
) -> dict[str, object]:
    """Fills priced below `threshold`: timing, clean size (total and by bucket),
    ambiguous size, and an upper bound that includes ambiguous groups."""
    stale = levels.filter(pl.col("price") < threshold)
    secs = [(ts - at).total_seconds() for ts in stale["ts"]]
    clean = stale.filter(~pl.col("ambiguous"))
    clean_secs = [(ts - at).total_seconds() for ts in clean["ts"]]
    out: dict[str, object] = {
        f"{prefix}_fills": stale.height,
        f"{prefix}_first_s": min(secs) if secs else None,
        f"{prefix}_last_s": max(secs) if secs else None,
        f"{prefix}_qty": float(clean["qty"].sum()),
        f"{prefix}_cost": float((clean["qty"] * clean["price"]).sum()),
        f"{prefix}_ambiguous_qty": float(stale.filter(pl.col("ambiguous"))["qty"].sum()),
        f"{prefix}_qty_upper": float(stale["qty"].sum()),
    }
    for name, lo, hi in BUCKETS:
        out[f"{prefix}_qty_{name}"] = sum(
            q for s, q in zip(clean_secs, clean["qty"], strict=True) if lo <= s < hi
        )
    return out


def measure(
    trades: pl.DataFrame,
    side: Side,
    at: datetime,
    params: Params,
    expected_move: float | None = None,
    game_start: datetime | None = None,
    data_end: datetime | None = None,
) -> dict[str, object] | None:
    """Measure one market's favored side at moment `at`. `trades` holds that market's
    rows (ts, outcome_id, side, price, qty, cost, ambiguous). None without a side.
    In-game (at >= game_start), the post-news window is cut at kickoff + game_length;
    a moment whose settle window runs past `data_end` is excluded."""
    if side.favored is None:
        return None
    fav = side.favored.outcome_id
    takers = trades.filter((pl.col("side") == "TAKER") & (pl.col("outcome_id") == fav))
    out: dict[str, object] = {"favored": side.favored.name, "kind": side.kind}
    settle_end = at + params.settle
    if data_end is not None and settle_end > data_end:
        out["excluded"] = "trade data ends inside the settle window"
        return out
    if game_start is not None and at >= game_start:
        settle_end = min(settle_end, game_start + params.game_length)
        if settle_end <= at + params.window:
            out["excluded"] = "game ends before the post-news window"
            return out
    out["post_cut_short"] = settle_end < at + params.settle

    pre = last_price(takers, at - params.lookback, at)
    if pre is None:
        out["excluded"] = "no favored-side trade in lookback"
        return out
    pre_price, pre_ts = pre
    out["pre_price"] = pre_price
    out["pre_age_s"] = (at - pre_ts).total_seconds()
    if at - pre_ts > params.max_pre_age:
        out["excluded"] = "pre-news trade older than max_pre_age"
        return out
    out["excluded"] = None
    lead_at = at - params.drift_lead
    lead = last_price(takers, lead_at - params.lookback, lead_at, inclusive_end=True)
    out["lead_price"] = lead[0] if lead else None
    out["drift"] = None if lead is None else pre_price - lead[0]
    # Drift is only informative if the favored side traded in the last drift_lead.
    out["recent_pre_trade"] = pre_ts > lead_at

    # Ex post: the post-news price, the move, and the after-the-fact stale view.
    post = last_price(takers.filter(pl.col("ts") > at + params.window), at, settle_end, True)
    out["post_price"] = post[0] if post else None
    out["move"] = None if post is None else post[0] - pre_price
    out["moved"] = post is not None and post[0] - pre_price >= params.min_move
    levels = fills(trades, fav, at, at + params.window)
    if post is not None and post[0] > pre_price:
        out |= stale_stats(levels, (pre_price + post[0]) / 2, at, "xp")

    # At t0: the expected move, fit only on earlier, settled events.
    out["expected_move"] = expected_move
    if expected_move is not None and expected_move > 0:
        out |= stale_stats(levels, pre_price + expected_move / 2, at, "t0")
    return out


# Loading


def load_markets(conn: psycopg.Connection, market_ids: Sequence[str]) -> dict[str, MarketInfo]:
    out: dict[str, MarketInfo] = {}
    for mid, mtype, desc, outcomes in conn.execute(
        "SELECT market_id::text, market_type, description, outcomes FROM novig_markets"
        " WHERE market_id = ANY(%s::uuid[])",
        (list(market_ids),),
    ):
        out[mid] = MarketInfo(
            mid, mtype, desc, tuple(Outcome(o["outcomeId"], o["name"]) for o in outcomes)
        )
    return out


def load_news(
    conn: psycopg.Connection, model: str, model_version: str, statuses: Iterable[str]
) -> list[NewsEvent]:
    """Resolved (matched or team_only) subjects of posts with the given statuses."""
    rows = conn.execute(
        """
        SELECT p.post_id, p.t0, c.status_change, r.subject, r.event_id::text, e.starts_at,
               e.description, r.team, r.tier1, r.market_ids::text[]
        FROM entity_resolutions r
        JOIN classifications c ON c.post_id = r.post_id AND c.model_version = r.model_version
        JOIN posts p ON p.post_id = r.post_id
        JOIN novig_events e ON e.event_id = r.event_id
        WHERE c.model = %s AND r.model_version = %s AND c.status_change = ANY(%s)
          AND r.status IN ('matched', 'team_only') AND r.market_ids IS NOT NULL
        ORDER BY p.t0, p.post_id, r.subject
        """,
        (model, model_version, list(statuses)),
    ).fetchall()
    markets = load_markets(conn, sorted({m for r in rows for m in r[9]}))
    news = []
    for post_id, t0, status, subject, event_id, start, desc, team, tier1, mids in rows:
        teams = game_teams(desc)
        if teams is None:
            continue  # the resolver only picks games, so this does not happen
        news.append(
            NewsEvent(
                post_id, t0, status, subject, event_id, start, teams, team, tier1,
                tuple(markets[m] for m in mids if m in markets),
            )
        )  # fmt: skip
    return news


TRADE_SCHEMA: dict[str, pl.DataType] = {
    "ts": pl.Datetime("us", "UTC"),
    "market_id": pl.String(),
    "outcome_id": pl.String(),
    "side": pl.String(),
    "price": pl.Float64(),
    "qty": pl.Float64(),
    "cost": pl.Float64(),
    "ambiguous": pl.Boolean(),
}


def load_trades(
    conn: psycopg.Connection, market_ids: list[str], start: datetime, end: datetime
) -> pl.DataFrame:
    """Trade rows for these markets with start <= ts <= end, in file order."""
    rows = conn.execute(
        """
        SELECT ts, market_id::text, outcome_id::text, side, price, qty, cost, ambiguous
        FROM novig_trades
        WHERE market_id = ANY(%s::uuid[]) AND ts >= %s AND ts <= %s AND source = 'public_csv'
        ORDER BY ts, file_date, row_num
        """,
        (market_ids, start, end),
    ).fetchall()
    return pl.DataFrame(rows, schema=TRADE_SCHEMA, orient="row")


# One moment: a news event at t0, or a control moment


@dataclass(frozen=True)
class Moment:
    news: NewsEvent  # at, game, and markets below replace the news event's own
    at: datetime
    group: str  # news, control_post, control_placebo
    event_id: str
    game_start: datetime
    markets: tuple[MarketInfo, ...]
    tier1: bool  # at this moment, not at the news time
    game_teams: tuple[str, str]  # of this moment's game (a placebo game has another opponent)

    @classmethod
    def of(cls, news: NewsEvent, group: str) -> "Moment":
        return cls(
            news, news.t0, group, news.event_id, news.game_start, news.markets, news.tier1,
            news.game_teams,
        )  # fmt: skip

    def as_news(self) -> NewsEvent:
        n = self.news
        return NewsEvent(
            n.post_id, self.at, n.status, n.subject, self.event_id, self.game_start,
            self.game_teams, n.team, self.tier1, self.markets,
        )  # fmt: skip


ExpectedFn = Callable[[str], float | None]  # kind -> expected move


def measure_moment(
    conn: psycopg.Connection,
    m: Moment,
    params: Params,
    expected: ExpectedFn | None = None,
    data_end: datetime | None = None,
) -> list[dict[str, object]]:
    """Measure every market of a moment that has a favored side."""
    n = m.as_news()
    with_side = [s for s in (favored_side(n, mk) for mk in m.markets) if s.favored is not None]
    if not with_side:
        return []
    trades = load_trades(
        conn,
        [s.market.market_id for s in with_side],
        m.at - params.drift_lead - params.lookback,
        m.at + params.settle,
    )
    week = nfl_week(m.game_start, params)
    rows = []
    for s in with_side:
        exp = expected(s.kind) if expected else None
        market_trades = trades.filter(pl.col("market_id") == s.market.market_id)
        r = measure(market_trades, s, m.at, params, exp, m.game_start, data_end)
        if r is None:
            continue
        rows.append(
            {
                "group": m.group,
                "post_id": n.post_id,
                "status": n.status,
                "subject": n.subject,
                "tier1": n.tier1,
                "event_id": m.event_id,
                "moment": m.at,
                "pregame": m.at < m.game_start,
                "week": week,
                "market_id": s.market.market_id,
                "market_type": s.market.market_type,
            }
            | r
        )
    return rows


def drop_overlapping(
    news: Sequence[NewsEvent], params: Params
) -> tuple[list[NewsEvent], list[NewsEvent]]:
    """Split news into (kept, dropped). An event is dropped if another material post on
    the same game came before it within `lookback`: its pre-news price may already
    reflect the earlier news, and its fill window may overlap. Subjects of one post are
    not compared with each other."""
    kept: list[NewsEvent] = []
    dropped: list[NewsEvent] = []
    for n in news:
        earlier = any(
            o.event_id == n.event_id
            and o.post_id != n.post_id
            and n.t0 - params.lookback <= o.t0 < n.t0
            for o in news
        )
        (dropped if earlier else kept).append(n)
    return kept, dropped


# Placebo controls: same player and team, same time before kickoff, another week


def game_markets(conn: psycopg.Connection, idx: Index) -> dict[str, MarketInfo]:
    """Every indexed NFL game market (lines and props), for placebo moments."""
    ids = [m for ms in idx.game_lines.values() for m in ms]
    ids += [p.market_id for ps in idx.props.values() for p in ps]
    return load_markets(conn, ids)


def placebo_moments(
    news: NewsEvent,
    idx: Index,
    markets: dict[str, MarketInfo],
    posts_by_team: Mapping[str, Sequence[datetime]],
    posts_about_subject: Sequence[datetime],
    params: Params,
    data_end: datetime | None = None,
    traded: set[str] | None = None,
) -> tuple[list[Moment], str | None]:
    """Up to placebo_games moments: the same offset before kickoff in the team's nearest
    other regular-season games. A moment must be at least control_gap from every post
    about the subject or about a player on either team in that game, and (with
    data_end) the trade data must cover its settle window. Markets are limited to
    `traded`, as news markets are. The second value explains a shortfall."""
    if news.team is None:
        return [], "team unknown"
    offset = news.game_start - news.t0
    games = sorted(
        (
            g
            for g in idx.schedule.get(news.team, [])
            if g.event_id != news.event_id and nfl_week(g.starts_at, params) is not None
        ),
        key=lambda g: (abs(g.starts_at - news.game_start), g.event_id),
    )
    key = normalize(news.subject)
    moments: list[Moment] = []
    for g in games:
        at = g.starts_at - offset
        if data_end is not None and at + params.settle > data_end:
            continue
        avoid = [
            *posts_about_subject,
            *(t for team in g.teams for t in posts_by_team.get(team, [])),
        ]
        if any(abs(at - a) < params.control_gap for a in avoid):
            continue
        mids = [p.market_id for p in idx.props.get(key, []) if p.event_id == g.event_id]
        mids += idx.game_lines.get(g.event_id, [])
        ms = tuple(markets[m] for m in mids if m in markets and (traded is None or m in traded))
        tier1 = bool(idx.prior_games(news.subject, at))
        moments.append(
            Moment(news, at, "control_placebo", g.event_id, g.starts_at, ms, tier1, g.teams)
        )
        if len(moments) == params.placebo_games:
            return moments, None
    return moments, f"{len(moments)} of {params.placebo_games} placebo games"


# Expected moves for the t0 view (expanding window, regular season only)


@dataclass
class Fit:
    """Realized moves of regular-season news markets, usable once their settle window
    has closed: (settled_at, post_id, status, kind, tier1, move)."""

    rows: list[tuple[datetime, int, str, str, bool, float]] = field(default_factory=list)

    def add(self, row: dict[str, object], params: Params) -> None:
        move = row.get("move")
        if row.get("week") is None or not isinstance(move, float):
            return  # preseason, or no realized move
        if row.get("excluded") is not None or row.get("post_cut_short"):
            return  # only full, valid settle windows
        moment = row["moment"]
        assert isinstance(moment, datetime)  # noqa: S101  (row built by measure_moment)
        self.rows.append(
            (moment + params.settle, int(str(row["post_id"])), str(row["status"]),
             str(row["kind"]), bool(row["tier1"]), move)
        )  # fmt: skip

    def expected(
        self, at: datetime, status: str | None, kind: str, tier1: bool, min_events: int
    ) -> tuple[float | None, str]:
        """Median move of rows settled before `at`, at the most specific level with
        at least `min_events` distinct events: (status, kind, tier1), then status.
        With status None (control posts, which have no material status of their own):
        all material statuses pooled, (kind, tier1), then everything."""
        done = [r for r in self.rows if r[0] < at]
        Row = tuple[datetime, int, str, str, bool, float]
        levels: tuple[tuple[str, Callable[[Row], bool]], ...]
        if status is None:
            levels = (
                ("pooled_kind_tier", lambda r: r[3] == kind and r[4] == tier1),
                ("pooled", lambda r: True),
            )
        else:
            levels = (
                ("status_kind_tier", lambda r: r[2] == status and r[3] == kind and r[4] == tier1),
                ("status", lambda r: r[2] == status),
            )
        for level, keep in levels:
            rows = [r for r in done if keep(r)]
            if len({r[1] for r in rows}) >= min_events:
                return median(r[5] for r in rows), level
        return None, "too few earlier events"


def sampler() -> random.Random:
    return random.Random(20261006)  # noqa: S311  (sampling, not security)


# Summaries


def summarize(results: pl.DataFrame, by: list[str], view: Literal["xp", "t0"]) -> pl.DataFrame:
    """Per group, over markets the view measured: events, markets, share with stale
    fills, timing distribution, and stale size (clean, ambiguous, upper bound, buckets)."""
    p = view
    if f"{p}_fills" not in results.columns:
        return pl.DataFrame()
    rows = results.filter(pl.col(f"{p}_fills").is_not_null())
    return (
        rows.group_by(by)
        .agg(
            pl.col("post_id").n_unique().alias("events"),
            pl.len().alias("markets"),
            (pl.col(f"{p}_fills") > 0).mean().round(3).alias("share_stale"),
            pl.col(f"{p}_first_s").median().round(1).alias("first_s_p50"),
            pl.col(f"{p}_first_s").quantile(0.9).round(1).alias("first_s_p90"),
            pl.col(f"{p}_last_s").quantile(0.25).round(1).alias("last_s_p25"),
            pl.col(f"{p}_last_s").median().round(1).alias("last_s_p50"),
            pl.col(f"{p}_last_s").quantile(0.75).round(1).alias("last_s_p75"),
            pl.col(f"{p}_last_s").quantile(0.9).round(1).alias("last_s_p90"),
            pl.col(f"{p}_qty").sum().round(0).alias("qty"),
            pl.col(f"{p}_cost").sum().round(0).alias("cost_usd"),
            pl.col(f"{p}_ambiguous_qty").sum().round(0).alias("ambiguous_qty"),
            pl.col(f"{p}_qty_upper").sum().round(0).alias("qty_upper"),
            *(pl.col(f"{p}_qty_{b}").sum().round(0).alias(f"qty_{b}") for b, _, _ in BUCKETS),
        )
        .sort(by)
    )
