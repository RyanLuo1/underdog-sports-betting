"""Backfill an account's posts from X through the Apify tweet-scraper actor.

One actor run per week ("from:<account> since:<start> until:<end>"), one run at a time,
each capped by maxItems and maxTotalChargeUsd. Every run's cost goes to a ledger
(data/underdog/raw/runs.jsonl); the backfill stops before the ledger's total would pass
the budget. Raw dataset items are saved per week, and posts are upserted into `posts`,
deduped by post ID. A week is fetched once it has ended; the current, partial week is
fetched again on the next run.

The actor's rules: at least 50 tweets per query, one concurrent run, and no monitoring
(repeated short-interval queries). This is a one-off historical backfill.
"""

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from news_edge.core.clock import t0_from_post_id
from news_edge.sources import apify

log = logging.getLogger(__name__)

SCRAPED_VIA = "apify:apidojo/tweet-scraper"


class BackfillConfig(BaseModel):
    """The `underdog_backfill` section of config/default.yaml."""

    account: str = "UnderdogNFL"
    start: date = date(2026, 8, 4)
    actor: str = "apidojo/tweet-scraper"
    sort: str = "Latest + Top"
    max_items_per_week: int = Field(2500, ge=50)
    budget_usd: float = Field(10.0, gt=0)
    event_price_usd: float = 0.0004


class Post(BaseModel):
    post_id: int
    account: str
    text: str
    t0: datetime
    created_at: datetime | None
    is_retweet: bool
    is_quote: bool
    raw: dict[str, Any]


Week = tuple[date, date]  # [start, end): `until:` is exclusive


def weeks(start: date, today: date) -> list[Week]:
    """Weekly windows from start through today; the last one may be partial."""
    out = []
    day = start
    while day <= today:
        out.append((day, min(day + timedelta(days=7), today + timedelta(days=1))))
        day += timedelta(days=7)
    return out


def query(account: str, week: Week) -> str:
    return f"from:{account} since:{week[0].isoformat()} until:{week[1].isoformat()}"


def _created_at(value: str) -> datetime:
    """X's createdAt, e.g. "Fri Nov 24 17:49:36 +0000 2023", as an aware UTC datetime."""
    return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").astimezone(UTC)


def parse_post(item: dict[str, Any]) -> Post | None:
    """A Post from one actor dataset item, or None for a placeholder (no id)."""
    raw_id = item.get("id")
    if raw_id is None or not str(raw_id).isdigit():
        return None
    created = item.get("createdAt")
    author = item.get("author") or {}
    return Post(
        post_id=int(raw_id),
        account=str(author.get("userName") or ""),
        text=str(item.get("fullText") or item.get("text") or ""),
        t0=t0_from_post_id(int(raw_id)),
        created_at=_created_at(created) if created else None,
        is_retweet=bool(item.get("isRetweet")),
        is_quote=bool(item.get("isQuote")),
        raw=item,
    )


@dataclass
class Ledger:
    """Append-only record of actor runs, so cost and fetched weeks survive reruns."""

    path: Path
    runs: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        runs = []
        if path.exists():
            runs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return cls(path, runs)

    @property
    def spent_usd(self) -> float:
        return sum(float(r.get("cost_usd", 0)) for r in self.runs)

    def fetched(self, account: str, week: Week) -> bool:
        """True if this complete week already has a successful run."""
        return any(
            r["account"] == account
            and r["since"] == week[0].isoformat()
            and r["until"] == week[1].isoformat()
            and r["status"] == "SUCCEEDED"
            and r["complete_week"]
            for r in self.runs
        )

    def append(self, entry: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        self.runs.append(entry)


@dataclass
class BackfillSummary:
    fetched: list[Week] = field(default_factory=list)
    skipped: list[Week] = field(default_factory=list)
    cost_usd: float = 0.0
    posts: int = 0
    stopped: str | None = None
    warnings: list[str] = field(default_factory=list)


def store_posts(conn: psycopg.Connection, posts: list[Post], fetched_at: datetime) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO posts (post_id, account, text, t0, created_at, is_retweet, is_quote,
                               raw, scraped_via, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (post_id) DO UPDATE SET
                text = EXCLUDED.text, raw = EXCLUDED.raw, fetched_at = EXCLUDED.fetched_at
            """,
            [
                (
                    p.post_id,
                    p.account,
                    p.text,
                    p.t0,
                    p.created_at,
                    p.is_retweet,
                    p.is_quote,
                    Jsonb(p.raw),
                    SCRAPED_VIA,
                    fetched_at,
                )
                for p in posts
            ],
        )


def run(
    http: httpx.Client,
    conn: psycopg.Connection,
    cfg: BackfillConfig,
    raw_dir: Path,
    today: date,
    max_weeks: int | None = None,
    windows: list[Week] | None = None,
) -> BackfillSummary:
    """Fetch every unfetched week, oldest first, at most max_weeks of them. With
    `windows`, fetch exactly those [since, until) windows instead (to re-check a gap)."""
    ledger = Ledger.load(raw_dir / "runs.jsonl")
    summary = BackfillSummary()
    min_run_usd = 50 * cfg.event_price_usd
    for week in windows or weeks(cfg.start, today):
        if windows is None and ledger.fetched(cfg.account, week):
            summary.skipped.append(week)
            continue
        if max_weeks is not None and len(summary.fetched) >= max_weeks:
            break
        remaining = cfg.budget_usd - ledger.spent_usd
        if remaining < min_run_usd:
            summary.stopped = (
                f"budget: ${ledger.spent_usd:.2f} of ${cfg.budget_usd:.2f} spent; stopping"
            )
            break
        cap = min(remaining, cfg.max_items_per_week * cfg.event_price_usd * 1.25 + 0.05)
        run_input = {
            "searchTerms": [query(cfg.account, week)],
            "sort": cfg.sort,
            "maxItems": cfg.max_items_per_week,
        }
        started = datetime.now(tz=UTC)
        result = apify.run_actor(
            http,
            cfg.actor,
            run_input,
            max_items=cfg.max_items_per_week,
            max_charge_usd=cap,
            fallback_event_price_usd=cfg.event_price_usd,
        )
        posts = {p.post_id: p for p in map(parse_post, result.items) if p is not None}
        other = Counter(
            p.account for p in posts.values() if p.account.lower() != cfg.account.lower()
        )
        mine = [p for p in posts.values() if p.account.lower() == cfg.account.lower()]
        complete = week[1] <= today  # `until` is exclusive, so the week has ended
        _save_raw(raw_dir / cfg.account / f"{week[0]}_{week[1]}.json", result.items)
        store_posts(conn, mine, started)
        ledger.append(
            {
                "account": cfg.account,
                "since": week[0].isoformat(),
                "until": week[1].isoformat(),
                "run_id": result.run_id,
                "status": result.status,
                "items": len(result.items),
                "unique_posts": len(mine),
                "other_authors": dict(other),
                "cost_usd": round(result.cost_usd, 4),
                "charged_events": result.charged_events,
                "complete_week": complete,
                "started_at": started.isoformat(),
            }
        )
        summary.fetched.append(week)
        summary.cost_usd += result.cost_usd
        summary.posts += len(mine)
        log.info(
            "%s..%s: %s, %d items, %d unique posts, $%.4f (total $%.2f of $%.2f)",
            week[0],
            week[1],
            result.status,
            len(result.items),
            len(mine),
            result.cost_usd,
            ledger.spent_usd,
            cfg.budget_usd,
        )
        if result.status != "SUCCEEDED":
            summary.warnings.append(f"{week[0]}: run {result.run_id} ended {result.status}")
        if len(result.items) >= cfg.max_items_per_week:
            summary.warnings.append(f"{week[0]}: hit maxItems; the week may be truncated")
        if 0 < len(mine) < 50 and complete:
            summary.warnings.append(f"{week[0]}: only {len(mine)} posts (actor minimum is 50)")
        if other:
            summary.warnings.append(f"{week[0]}: posts by other accounts ignored: {dict(other)}")
        if ledger.spent_usd > cfg.budget_usd:
            summary.stopped = f"budget exceeded: ${ledger.spent_usd:.2f}; stopping"
            break
    return summary


def _save_raw(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(items))
    tmp.replace(path)


def weekly_counts(conn: psycopg.Connection, account: str, start: date, today: date) -> list[str]:
    """One report line per week: posts stored, with retweets and quotes broken out."""
    lines = []
    for week in weeks(start, today):
        row = conn.execute(
            """
            SELECT count(*), count(*) FILTER (WHERE is_retweet), count(*) FILTER (WHERE is_quote)
            FROM posts
            WHERE lower(account) = lower(%s) AND t0 >= %s AND t0 < %s
            """,
            (account, week[0], week[1]),
        ).fetchone()
        n, rt, qt = row if row else (0, 0, 0)
        lines.append(f"{week[0]}..{week[1]}  {n:5d} posts  ({rt} retweets, {qt} quotes)")
    return lines
