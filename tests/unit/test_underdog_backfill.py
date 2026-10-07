import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from news_edge.core.clock import X_EPOCH_MS, t0_ms_from_post_id
from news_edge.sources import apify
from news_edge.sources import underdog_backfill as ub

TOKEN = "apify_api_SECRET123"  # noqa: S105


def post_id(when: datetime, low: int = 0) -> int:
    return ((int(when.timestamp() * 1000) - X_EPOCH_MS) << 22) | low


def item(pid: int, author: str = "UnderdogNFL", **kw: Any) -> dict[str, Any]:
    return {
        "id": str(pid),
        "fullText": f"post {pid}",
        "createdAt": "Tue Aug 04 17:00:00 +0000 2026",
        "isRetweet": False,
        "isQuote": False,
        "author": {"userName": author},
        **kw,
    }


class FakeApify:
    """Serves one run per POST; each run's dataset is the next entry in `datasets`."""

    def __init__(self, datasets: list[list[dict[str, Any]]], events_per_run: int | None = None):
        self.datasets = datasets
        self.events = events_per_run
        self.started: list[dict[str, Any]] = []
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        path = request.url.path
        if request.method == "POST":
            n = len(self.started)
            self.started.append(
                {"input": json.loads(request.content), "params": dict(request.url.params)}
            )
            items = self.datasets[n]
            run = {
                "id": f"run{n}",
                "status": "SUCCEEDED",
                "defaultDatasetId": f"ds{n}",
                "chargedEventCounts": {
                    "apify-default-dataset-item": self.events
                    if self.events is not None
                    else len(items)
                },
                "pricingInfo": {
                    "pricingPerEvent": {
                        "actorChargeEvents": {
                            "apify-default-dataset-item": {"eventPriceUsd": 0.0004}
                        }
                    }
                },
                "usageTotalUsd": 0.001,
            }
            return httpx.Response(201, json={"data": run})
        if path.startswith("/v2/datasets/"):
            n = int(path.split("/")[3].removeprefix("ds"))
            offset = int(request.url.params["offset"])
            limit = int(request.url.params["limit"])
            return httpx.Response(200, json=self.datasets[n][offset : offset + limit])
        raise AssertionError(path)


def _client(fake: FakeApify) -> httpx.Client:
    http = apify.client(SecretStr(TOKEN))
    http._transport = httpx.MockTransport(fake)
    return http


def test_weeks_cover_start_to_today() -> None:
    w = ub.weeks(date(2026, 8, 4), date(2026, 8, 20))
    assert w == [
        (date(2026, 8, 4), date(2026, 8, 11)),
        (date(2026, 8, 11), date(2026, 8, 18)),
        (date(2026, 8, 18), date(2026, 8, 21)),  # partial, through today
    ]
    assert ub.query("UnderdogNFL", w[0]) == "from:UnderdogNFL since:2026-08-04 until:2026-08-11"


def test_parse_post_decodes_t0_and_created_at() -> None:
    when = datetime(2026, 8, 4, 17, 0, 0, 123000, tzinfo=UTC)
    pid = post_id(when, low=7)
    post = ub.parse_post(item(pid, isQuote=True))
    assert post is not None
    assert post.t0 == when
    assert post.created_at == datetime(2026, 8, 4, 17, 0, 0, tzinfo=UTC)
    assert post.is_quote and not post.is_retweet
    assert ub.parse_post({"noResults": True}) is None


def test_run_cost_uses_listed_price_and_usage() -> None:
    run = {
        "chargedEventCounts": {"apify-default-dataset-item": 1000},
        "pricingInfo": {
            "pricingPerEvent": {
                "actorChargeEvents": {
                    "apify-default-dataset-item": {
                        "eventTieredPricingUsd": {"FREE": {"tieredEventPriceUsd": 0.0004}}
                    }
                }
            }
        },
        "usageTotalUsd": 0.01,
    }
    assert apify.run_cost(run, fallback_event_price_usd=1) == pytest.approx(0.41)
    assert apify.run_cost({"chargedEventCounts": {"x": 10}}, 0.5) == pytest.approx(5)


@pytest.fixture
def cfg() -> ub.BackfillConfig:
    return ub.BackfillConfig(start=date(2026, 8, 4), max_items_per_week=100, budget_usd=10)


@pytest.mark.db
def test_backfill_stores_dedupes_and_skips_fetched_weeks(
    db: psycopg.Connection, tmp_path: Path, cfg: ub.BackfillConfig
) -> None:
    a = post_id(datetime(2026, 8, 5, tzinfo=UTC))
    b = post_id(datetime(2026, 8, 6, tzinfo=UTC))
    c = post_id(datetime(2026, 8, 12, tzinfo=UTC))
    week1 = [item(a), item(b), item(a), item(9, author="someone"), {"noResults": True}]
    fake = FakeApify([week1, [item(c)]])
    today = date(2026, 8, 17)  # week 2 is partial
    with _client(fake) as http:
        summary = ub.run(http, db, cfg, tmp_path, today)

    assert summary.fetched == ub.weeks(cfg.start, today)
    assert summary.posts == 3
    first = fake.started[0]
    assert first["input"] == {
        "searchTerms": ["from:UnderdogNFL since:2026-08-04 until:2026-08-11"],
        "sort": "Latest + Top",
        "maxItems": 100,
    }
    assert first["params"]["maxItems"] == "100"
    assert float(first["params"]["maxTotalChargeUsd"]) <= 10
    assert all(TOKEN not in url for url in fake.urls)
    assert db.execute("SELECT count(*) FROM posts").fetchone() == (3,)
    row = db.execute("SELECT t0 FROM posts WHERE post_id = %s", (a,)).fetchone()
    assert row is not None
    assert int(row[0].timestamp() * 1000) == t0_ms_from_post_id(a)
    assert (tmp_path / "UnderdogNFL" / "2026-08-04_2026-08-11.json").exists()
    assert any("other accounts" in w for w in summary.warnings)

    # Rerun: the complete week is skipped, the partial one is fetched again.
    fake2 = FakeApify([[item(c)]])
    with _client(fake2) as http:
        again = ub.run(http, db, cfg, tmp_path, today)
    assert again.skipped == [ub.weeks(cfg.start, today)[0]]
    assert len(fake2.started) == 1
    assert db.execute("SELECT count(*) FROM posts").fetchone() == (3,)

    counts = ub.weekly_counts(db, "UnderdogNFL", cfg.start, today)
    assert counts[0].startswith("2026-08-04..2026-08-11      2 posts")


@pytest.mark.db
def test_backfill_stops_at_budget(db: psycopg.Connection, tmp_path: Path) -> None:
    cfg = ub.BackfillConfig(start=date(2026, 8, 4), max_items_per_week=100, budget_usd=1.0)
    # Each run reports 2000 charged events ($0.80 + $0.001 usage).
    fake = FakeApify([[item(1 << 40)], [item(1 << 41)], [item(1 << 42)]], events_per_run=2000)
    with _client(fake) as http:
        summary = ub.run(http, db, cfg, tmp_path, date(2026, 8, 30))
    # Each run is capped at maxItems x price x 1.25 + $0.05 = $0.10 on Apify's side (this
    # fake ignores the cap). After run 1, $0.199 remains, so run 2 starts; the total then
    # passes $1 and the backfill stops.
    assert len(fake.started) == 2
    assert [float(r["params"]["maxTotalChargeUsd"]) for r in fake.started] == [0.1, 0.1]
    assert summary.stopped is not None and "budget" in summary.stopped
    ledger = ub.Ledger.load(tmp_path / "runs.jsonl")
    assert ledger.spent_usd == pytest.approx(1.602)

    # A later rerun does not start any run: the ledger is already over budget.
    fake2 = FakeApify([[item(1 << 43)]])
    with _client(fake2) as http:
        assert ub.run(http, db, cfg, tmp_path, date(2026, 8, 30)).stopped is not None
    assert fake2.started == []


@pytest.mark.db
def test_backfill_max_weeks(db: psycopg.Connection, tmp_path: Path, cfg: ub.BackfillConfig) -> None:
    fake = FakeApify([[item(1 << 40)], [item(1 << 41)]])
    with _client(fake) as http:
        summary = ub.run(http, db, cfg, tmp_path, date(2026, 8, 30), max_weeks=1)
    assert len(fake.started) == 1
    assert len(summary.fetched) == 1


def test_start_failure_never_includes_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"type": "x", "message": "plan required"}})

    http = apify.client(SecretStr(TOKEN))
    http._transport = httpx.MockTransport(handler)
    with http, pytest.raises(apify.ApifyError) as err:
        apify.run_actor(http, "a/b", {}, 10, 1.0, 0.0004)
    assert TOKEN not in str(err.value)
    assert "plan required" in str(err.value)
