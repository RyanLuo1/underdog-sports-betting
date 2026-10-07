"""Minimal Apify API client for running an actor and reading its dataset.

The token goes in the Authorization header, never in a URL, so request logs and
errors cannot leak it.

API: https://docs.apify.com/api/v2
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr

log = logging.getLogger(__name__)

API = "https://api.apify.com"
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"})


class ApifyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    dataset_id: str
    items: list[dict[str, Any]]
    cost_usd: float
    charged_events: dict[str, int]


def client(token: SecretStr, timeout: float = 120) -> httpx.Client:
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {token.get_secret_value()}"},
        timeout=timeout,
    )


def run_actor(
    http: httpx.Client,
    actor: str,
    run_input: dict[str, Any],
    max_items: int,
    max_charge_usd: float,
    fallback_event_price_usd: float,
    poll_seconds: float = 10,
) -> RunResult:
    """Start an actor run capped at max_items and max_charge_usd, wait for it, and return
    its dataset items and cost. Raises ApifyError if the run cannot start."""
    resp = http.post(
        f"/v2/acts/{actor.replace('/', '~')}/runs",
        params={"maxItems": max_items, "maxTotalChargeUsd": f"{max_charge_usd:.2f}"},
        json=run_input,
    )
    if resp.status_code >= 400:
        raise ApifyError(f"start failed: HTTP {resp.status_code} {_error(resp)}")
    run = resp.json()["data"]
    while run["status"] not in TERMINAL:
        time.sleep(poll_seconds)
        resp = http.get(f"/v2/actor-runs/{run['id']}", params={"waitForFinish": 60})
        resp.raise_for_status()
        run = resp.json()["data"]
    items = dataset_items(http, run["defaultDatasetId"])
    return RunResult(
        run_id=run["id"],
        status=run["status"],
        dataset_id=run["defaultDatasetId"],
        items=items,
        cost_usd=run_cost(run, fallback_event_price_usd),
        charged_events=dict(run.get("chargedEventCounts") or {}),
    )


def dataset_items(http: httpx.Client, dataset_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset, limit = 0, 1000
    while True:
        resp = http.get(
            f"/v2/datasets/{dataset_id}/items",
            params={"format": "json", "clean": "true", "offset": offset, "limit": limit},
        )
        resp.raise_for_status()
        page: list[dict[str, Any]] = resp.json()
        items.extend(page)
        if len(page) < limit:
            return items
        offset += limit


def run_cost(run: dict[str, Any], fallback_event_price_usd: float) -> float:
    """What a run cost, erring high: charged events at their listed price (or the
    fallback price), plus any platform usage billed to us."""
    prices = _event_prices(run.get("pricingInfo") or {})
    events = run.get("chargedEventCounts") or {}
    charged = sum(n * prices.get(e, fallback_event_price_usd) for e, n in events.items())
    return float(charged) + float(run.get("usageTotalUsd") or 0.0)


def _event_prices(pricing: dict[str, Any]) -> dict[str, float]:
    events = (pricing.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}
    prices: dict[str, float] = {}
    for name, event in events.items():
        price = event.get("eventPriceUsd")
        if price is None:
            tiers = event.get("eventTieredPricingUsd") or {}
            tier_prices = [t.get("tieredEventPriceUsd") for t in tiers.values()]
            price = max((p for p in tier_prices if p is not None), default=None)
        if price is not None:
            prices[name] = float(price)
    return prices


def _error(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return f"{err.get('type', '')}: {err.get('message', '')}"
    except ValueError:
        return resp.text[:200]
