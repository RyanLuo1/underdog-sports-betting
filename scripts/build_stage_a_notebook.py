"""Write src/news_edge/research/notebooks/stage_a.ipynb (no outputs) and, with --run,
an executed copy.

The repo is public, so results stay out of it: the committed notebook has no outputs,
and the executed copy goes to docs/private/ (gitignored) unless --out says otherwise.
The notebook only calls news_edge.research.stage_a; the logic and its tests live there.

    uv run --group research python scripts/build_stage_a_notebook.py          # notebook only
    uv run --group research python scripts/build_stage_a_notebook.py --run    # + results
"""

import argparse
import sys
from pathlib import Path

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

PATH = Path("src/news_edge/research/notebooks/stage_a.ipynb")
RESULTS = Path("docs/private/stage_a_results.ipynb")

INTRO = """# Stage A: do Novig prices stay stale after NFL injury news?

Trades only, NFL only, @UnderdogNFL posts since Aug 4, 2026. Method: `docs/private/spec.md`
(Stage A). Logic: `news_edge.research.stage_a` (unit-tested); this notebook only runs it.

**Events:** posts classified (rules parser) as out, doubtful, inactive, IR, or in-game injury,
with the player resolved to his team's next game from the schedule. **Markets and sides** come
from the news and market names only: Under on the player's props (excluded pre-game for out,
inactive, and IR, which are expected to void), the opponent on spreads and moneylines (incl.
first-half), Under on his team's total.

**Prices** are the favored side's ask (what a taker paid). Pre-news: the last millisecond of
favored-side TAKER trades strictly before t0, at most 30 minutes old. Sweeps are split into
maker levels; ambiguous groups count for timing, are reported separately, and are added only
in the upper-bound size.

**Two views.**
- **After-the-fact (`xp_` columns, ex post):** post-news price = last favored trade 10-60
  minutes after t0. "Moved" = it rose at least 3 cents. A fill is stale if closer to the
  pre-news than the post-news price. Uses prices after t0: a measurement, never a signal input.
- **At t0 (`t0_` columns):** no price filter. Expected move = median move of earlier
  regular-season events whose 60-minute window closed before this t0 (at least 5 events;
  status x kind x tier, else status). A fill is stale if closer to the pre-news price than to
  pre-news + expected move. Reported from Week 2.

**Events are incidents:** a post that follows another material post on the same game within
60 minutes is dropped (its pre-news price may already be post-news). In-game, the post-news
window ends at kickoff + 3.5 h so it is not the final result; moments whose settle window
runs past the end of the trade data are excluded.

**Controls:** good-news posts (probable, active, full practice; t0 view uses the material
fit pooled across statuses), and placebo moments: the same player and team, the same time
before kickoff, in up to 3 other regular-season games, at least 60 minutes from any
classified post about that player or a player on either team. Every table is split
pre-game / in-game.

Placeholders, not derived from data: the 10-minute window, the 3-cent move, the 30-minute
age limit, the 5-event minimum, and the 3.5-hour game length."""

SETUP = """from collections import Counter

import polars as pl

from news_edge.classify import rules
from news_edge.core.db import connect
from news_edge.entities.store import load_index
from news_edge.research import stage_a as sa

pl.Config.set_tbl_rows(60)
pl.Config.set_tbl_cols(30)
pl.Config.set_tbl_width_chars(220)
pl.Config.set_fmt_str_lengths(36)

P = sa.Params()
BY = ["pregame", "group", "status", "kind"]
conn = connect()
news_all = sa.load_news(conn, rules.MODEL, rules.VERSION, sa.MATERIAL)
good = sa.load_news(conn, rules.MODEL, rules.VERSION, sa.CONTROL)
idx = load_index(conn, "NFL")
game_markets = sa.game_markets(conn, idx)
data_end = conn.execute("SELECT max(ts) FROM novig_trades").fetchone()[0]
traded = {
    str(r[0])
    for r in conn.execute("SELECT DISTINCT market_id FROM novig_trades WHERE league = 'NFL'")
}
# Every classified post (any status but unknown): who and which team, for placebo avoid lists.
classified = conn.execute(
    "SELECT p.t0, c.players, coalesce(c.team, r.team)"
    " FROM classifications c JOIN posts p USING (post_id)"
    " LEFT JOIN entity_resolutions r"
    "   ON r.post_id = c.post_id AND r.model_version = c.model_version"
    " WHERE c.model_version = %s AND c.status_change <> 'unknown'",
    (rules.VERSION,),
).fetchall()
posts_by_team, posts_by_player = {}, {}
for t0, players, team in classified:
    if team:
        posts_by_team.setdefault(team, []).append(t0)
    for name in players or []:
        posts_by_player.setdefault(sa.normalize(name), []).append(t0)
news, overlapping = sa.drop_overlapping(news_all, P)
print(f"classifier {rules.MODEL} {rules.VERSION}")
print(P)
print(f"material subjects: {len(news_all)}; kept as incidents: {len(news)};"
      f" dropped (earlier material post on the same game within 60 min): {len(overlapping)}")
print(f"good-news subjects: {len(good)}; trade data ends {data_end}")"""

COVERAGE = """resolution = pl.DataFrame(
    conn.execute(
        '''SELECT c.status_change AS status, r.status AS resolution,
                  r.reason LIKE 'no team: post names none%%' AS no_history_no_team,
                  count(*) AS subjects
           FROM entity_resolutions r JOIN classifications c USING (post_id, model_version)
           WHERE r.model_version = %s AND c.status_change = ANY(%s)
           GROUP BY 1, 2, 3 ORDER BY 1, 2, 3''',
        (rules.VERSION, list(sa.MATERIAL)),
    ).fetchall(),
    schema=["status", "resolution", "no_history_no_team", "subjects"],
    orient="row",
)
print("Skipped because the player had no prop history before t0 and the post named no team:",
      resolution.filter(pl.col("no_history_no_team"))["subjects"].sum())
resolution"""

SIDES = """reasons = Counter()
for n in news:
    for m in n.markets:
        s = sa.favored_side(n, m)
        reasons[(s.kind, "has side" if s.favored else s.reason)] += 1
pl.DataFrame([{"kind": k, "result": r, "markets": c} for (k, r), c in reasons.most_common()])"""

RUN = """fit = sa.Fit()
fit_levels = Counter()
rows = []

def expected_for(status, tier1, game_start, at):
    # Point-in-time expected move per kind, from news settled before `at` (Week 2 on).
    if (sa.nfl_week(game_start, P) or 0) < 2:
        return None
    def fn(kind):
        value, level = fit.expected(at, status, kind, tier1, P.fit_min_events)
        fit_levels[level] += 1
        return value
    return fn

# News in time order: each event's expected move uses only events settled before its t0.
for n in sorted(news, key=lambda n: (n.t0, n.post_id, n.subject)):
    exp = expected_for(n.status, n.tier1, n.game_start, n.t0)
    measured = sa.measure_moment(conn, sa.Moment.of(n, "news"), P, exp, data_end)
    rows += measured
    for r in measured:
        fit.add(r, P)

for n in good:  # control posts: the material fit pooled across statuses
    exp = expected_for(None, n.tier1, n.game_start, n.t0)
    rows += sa.measure_moment(conn, sa.Moment.of(n, "control_post"), P, exp, data_end)

shortfall = Counter()
for n in news:
    about = posts_by_player.get(sa.normalize(n.subject), [])
    moments, short = sa.placebo_moments(
        n, idx, game_markets, posts_by_team, about, P, data_end, traded
    )
    shortfall[short or "full"] += 1
    for m in moments:
        exp = expected_for(n.status, m.tier1, m.game_start, m.at)
        rows += sa.measure_moment(conn, m, P, exp, data_end)

results = pl.DataFrame(rows, infer_schema_length=None)
print("placebo moments per news incident:", dict(shortfall))
print("t0-view fit level used per market:", dict(fit_levels))
results.group_by("group", "pregame", "excluded").agg(
    pl.col("post_id").n_unique().alias("events"), pl.len().alias("markets")
).sort("group", "pregame", "excluded")"""

DRIFT = """# Ex post. Only markets that moved, whose favored side traded in the last 10 minutes
# before t0 (else drift is 0 by construction), and whose post price is above the
# t0-10min price. share = (pre - lead) / (post - lead), clipped to [0, 1] for the median.
drift = results.filter(
    (pl.col("group") == "news") & pl.col("excluded").is_null() & pl.col("moved")
    & pl.col("lead_price").is_not_null() & pl.col("recent_pre_trade")
    & (pl.col("post_price") > pl.col("lead_price"))
).with_columns(
    ((pl.col("pre_price") - pl.col("lead_price"))
     / (pl.col("post_price") - pl.col("lead_price"))).clip(0, 1).alias("share_before_t0")
)
print("moved news markets:",
      results.filter((pl.col("group") == "news") & pl.col("moved")).height,
      "| with a favored trade in the last 10 min before t0:", drift.height)
drift.group_by("pregame", "status").agg(
    pl.col("post_id").n_unique().alias("events"),
    pl.len().alias("markets"),
    ((pl.col("pre_price") - pl.col("lead_price")).sum()
     / (pl.col("post_price") - pl.col("lead_price")).sum()).round(3).alias("aggregate_share"),
    pl.col("share_before_t0").median().round(3).alias("median_share"),
    (pl.col("share_before_t0") >= 0.5).mean().round(3).alias("share_mostly_before_t0"),
).sort("pregame", "status")"""

PER_EVENT = """per_event = (
    results.filter((pl.col("group") == "news") & pl.col("excluded").is_null())
    .group_by("post_id", "status", "subject", "moment", "pregame", "week")
    .agg(
        pl.len().alias("markets"),
        pl.col("moved").sum().alias("moved"),
        (pl.col("xp_fills") > 0).sum().alias("xp_markets_stale"),
        pl.col("xp_first_s").min().alias("xp_first_s"),
        pl.col("xp_last_s").max().alias("xp_last_s"),
        pl.col("xp_qty").sum().round(0).alias("xp_qty"),
        pl.col("xp_qty_upper").sum().round(0).alias("xp_qty_upper"),
    )
    .sort("xp_qty_upper", descending=True, nulls_last=True)
)
per_event.head(40)"""

MOVED = """def moved_only(df):
    return df.filter(pl.col("moved") & pl.col("excluded").is_null())

regular = results.filter(pl.col("week").is_not_null())
sa.summarize(moved_only(regular), BY, "xp")"""

UNFILTERED = """priced = regular.filter(pl.col("excluded").is_null())
print(priced.group_by("group", "pregame").agg(
    pl.len().alias("priced_markets"),
    pl.col("xp_fills").is_not_null().sum().alias("with_ex_post_stats"),
    pl.col("moved").sum().alias("moved"),
).sort("group", "pregame"))
sa.summarize(priced, ["pregame", "group"], "xp")"""

T0_VIEW = """week2 = results.filter(pl.col("week") >= 2)
print(week2.filter(pl.col("excluded").is_null()).group_by(BY).agg(
    pl.len().alias("priced"),
    pl.col("expected_move").is_not_null().sum().alias("has_expected_move"),
    (pl.col("expected_move") > 0).sum().alias("t0_measured"),
).sort(BY))
t0_view = sa.summarize(week2.filter(pl.col("excluded").is_null()), BY, "t0")
xp_same_events = sa.summarize(moved_only(week2), BY, "xp")
print("At t0 (point in time), Week 2 on:")
display(t0_view)
print("After the fact, the same Week 2+ events (moved only):")
xp_same_events"""

PRESEASON = """sa.summarize(moved_only(results.filter(pl.col("week").is_null())), BY, "xp")"""

MARKET_TYPES = """by = ["pregame", "group", "market_type"]
by_type = sa.summarize(moved_only(regular), by, "xp")
by_type.filter(pl.col("markets") >= 5)"""

RESULTS_INTRO = """# Results

In order: (1) drift before t0, (2) stale fills after news vs placebo weeks, (3) timing,
(4) stale size per event, (5) t0 view vs after-the-fact view. (6) pre-game vs in-game is a
column in every table. An "event" is a news incident, a good-news post, or a placebo moment;
`events` counts them, `markets` counts market measurements. Ex-post tables use markets that
moved (rose at least 3 cents) unless noted."""

RESULTS_HELPERS = """def events_view(df, view):
    # One row per event (post or placebo moment): did any market have stale fills, the
    # earliest and latest stale fill, and total stale size (clean, upper bound).
    return (
        df.filter(pl.col(f"{view}_fills").is_not_null())
        .group_by("group", "pregame", "post_id", "moment")
        .agg(
            pl.len().alias("markets"),
            (pl.col(f"{view}_fills") > 0).any().alias("stale"),
            pl.col(f"{view}_first_s").min().alias("first_s"),
            pl.col(f"{view}_last_s").max().alias("last_s"),
            pl.col(f"{view}_qty").sum().alias("qty"),
            pl.col(f"{view}_cost").sum().alias("cost"),
            pl.col(f"{view}_qty_upper").sum().alias("qty_upper"),
        )
    )

def q(col, p):
    return pl.col(col).quantile(p).round(1)

xp_events = events_view(moved_only(regular), "xp")
t0_events = events_view(week2.filter(pl.col("excluded").is_null()), "t0")"""

Q1 = """# 1. Drift before t0 (ex post): pooled over statuses
drift.group_by("pregame").agg(
    pl.col("post_id").n_unique().alias("events"),
    pl.len().alias("markets"),
    ((pl.col("pre_price") - pl.col("lead_price")).sum()
     / (pl.col("post_price") - pl.col("lead_price")).sum()).round(3).alias("aggregate_share"),
    pl.col("share_before_t0").median().round(3).alias("median_share"),
).sort("pregame")"""

Q2 = """# 2. Stale fills: news vs placebo weeks vs good-news posts (ex post, moved markets)
xp_events.group_by("pregame", "group").agg(
    pl.len().alias("events"),
    pl.col("markets").sum().alias("markets"),
    pl.col("stale").mean().round(3).alias("share_events_stale"),
).sort("pregame", "group")"""

Q2_MARKETS = """# 2b. Same comparison per market, and without the 3-cent filter (any rise)
any_rise = regular.filter(pl.col("excluded").is_null() & pl.col("xp_fills").is_not_null())
pl.concat([
    moved_only(regular).group_by("pregame", "group").agg(
        pl.lit("moved >= 3c").alias("filter"), pl.col("post_id").n_unique().alias("posts"),
        pl.len().alias("markets"), (pl.col("xp_fills") > 0).mean().round(3).alias("share_stale")),
    any_rise.group_by("pregame", "group").agg(
        pl.lit("any rise").alias("filter"), pl.col("post_id").n_unique().alias("posts"),
        pl.len().alias("markets"), (pl.col("xp_fills") > 0).mean().round(3).alias("share_stale")),
]).sort("filter", "pregame", "group")"""

Q3 = """# 3. Timing of stale fills, seconds after t0, over events with any stale fill (ex post)
xp_events.filter(pl.col("stale")).group_by("pregame", "group").agg(
    pl.len().alias("events"),
    q("first_s", 0.5).alias("first_p50"), q("first_s", 0.9).alias("first_p90"),
    q("last_s", 0.5).alias("last_p50"), q("last_s", 0.9).alias("last_p90"),
).sort("pregame", "group")"""

Q4 = """# 4. Stale size per event (contracts; dollars paid), main estimate and upper bound
xp_events.group_by("pregame", "group").agg(
    pl.len().alias("events"),
    q("qty", 0.5).alias("qty_p50"), q("qty", 0.9).alias("qty_p90"),
    pl.col("qty").mean().round(0).alias("qty_mean"),
    q("cost", 0.5).alias("usd_p50"), q("cost", 0.9).alias("usd_p90"),
    q("qty_upper", 0.5).alias("upper_qty_p50"), q("qty_upper", 0.9).alias("upper_qty_p90"),
    pl.col("qty_upper").mean().round(0).alias("upper_qty_mean"),
).sort("pregame", "group")"""

Q5 = """# 5. t0 view vs after-the-fact view, Week 2 on, per event
xp_w2 = events_view(moved_only(week2), "xp")
def side(df, label):
    return df.group_by("pregame", "group").agg(
        pl.lit(label).alias("view"), pl.len().alias("events"),
        pl.col("stale").mean().round(3).alias("share_events_stale"),
        q("last_s", 0.5).alias("last_p50"), q("qty", 0.5).alias("qty_p50"),
        q("qty", 0.9).alias("qty_p90"), q("qty_upper", 0.9).alias("upper_qty_p90"),
    )
pl.concat([side(t0_events, "at t0"), side(xp_w2, "after the fact")]).sort(
    "pregame", "group", "view")"""

CAVEATS = """## Caveats

- **t0 is Underdog's post time.** Underdog aggregates; the original reporter often posts
  minutes earlier, so some of the move can predate t0 (see the drift table) and the stale
  window measured here is after the real news.
- **Ex-post columns** (`post_price`, `move`, `moved`, `drift` shares, every `xp_` column) use
  prices after t0. They describe events that turned out to move the market and overstate what
  a strategy acting at t0 could capture. The `t0_` view is the point-in-time counterpart.
- **Teammates' props** are not measured (no rule yet for their side). Pre-game doubtful
  players' own props are measured but often void; read them separately (status = doubtful).
- **Team for game lines** comes from the post, the player's prop history before t0, or (only
  to choose between the two teams of one prior game) Sleeper's current roster.
- **Multi-player posts** would measure the same game lines once per player; there are none
  in this data (one subject per post).
- **In-game** moves can be driven by game events; the placebo in-game moments are the check.
- **Counts:** a news subject has several markets (moneyline, spread, first half, team total,
  props); `events` counts posts, `markets` counts market measurements.
- Coverage follows the Novig history sync (back to 2026-07-25) and markets that traded."""


def cells() -> list[nbformat.NotebookNode]:
    md, code = new_markdown_cell, new_code_cell
    return [
        md(INTRO),
        code(SETUP),
        md("## Coverage\n\nResolution of material subjects, and why markets have no side."),
        code(COVERAGE),
        code(SIDES),
        md("## Run: news, good-news posts, placebo moments\n\nExclusions by group."),
        code(RUN),
        md("## Is Underdog first? Drift before t0 (ex post)"),
        code(DRIFT),
        md("## Per event (after the fact)"),
        code(PER_EVENT),
        md("## After the fact: markets that moved, regular season"),
        code(MOVED),
        md(
            "## After the fact, without the 3-cent filter, regular season\n\n"
            "Ex-post stats exist only where the post-news price rose (any amount), so this is "
            "still a price filter, just a weaker one. Counts below."
        ),
        code(UNFILTERED),
        md(
            "## At t0 vs after the fact, Week 2 on\n\n"
            "The t0 view covers every priced market with a positive expected move; the ex-post "
            "view below it is moved-only, so the market sets differ. Eligibility counts first."
        ),
        code(T0_VIEW),
        md("## Preseason, reported separately (after the fact, moved only)"),
        code(PRESEASON),
        md("## By market type (after the fact, moved only, regular season)"),
        code(MARKET_TYPES),
        md(RESULTS_INTRO),
        code(RESULTS_HELPERS),
        md("## 1. What share of the move happens before t0?"),
        code(Q1),
        md("## 2. Stale fills after news vs placebo weeks"),
        code(Q2),
        code(Q2_MARKETS),
        md("## 3. Timing: first and last stale fill"),
        code(Q3),
        md("## 4. Stale size per event: main estimate and upper bound"),
        code(Q4),
        md("## 5. t0-only view vs after-the-fact view (Week 2 on)"),
        code(Q5),
        md(CAVEATS),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", action="store_true", help="also write an executed copy")
    parser.add_argument("--out", type=Path, default=RESULTS, help="executed copy's path")
    args = parser.parse_args(argv)
    nb = new_notebook(cells=cells())
    nb.metadata["kernelspec"] = {
        "name": "python3",
        "display_name": "Python 3",
        "language": "python",
    }
    PATH.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, PATH)
    print(f"wrote {PATH} (no outputs)")
    if args.run:
        from nbclient import NotebookClient

        # Run from the repo root so .env and data/ resolve.
        NotebookClient(nb, timeout=3600, resources={"metadata": {"path": "."}}).execute()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        nbformat.write(nb, args.out)
        print(f"wrote {args.out} (executed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
