"""Backfill @UnderdogNFL posts from X via Apify, one week per actor run.

Reruns skip weeks already fetched, and the whole backfill stops before total spend (in
data/underdog/raw/runs.jsonl) passes underdog_backfill.budget_usd. Prints posts per
week at the end so gaps can be checked against the account's timeline.

    uv run python scripts/backfill_underdog_posts.py
    uv run python scripts/backfill_underdog_posts.py --weeks 1    # fetch at most 1 week
    uv run python scripts/backfill_underdog_posts.py --report     # counts only, no API
    uv run python scripts/backfill_underdog_posts.py --window 2026-08-28 2026-09-04
                                                       # refetch one window (gap check)
"""

import argparse
import fcntl
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from news_edge.core.db import connect
from news_edge.core.settings import get_settings
from news_edge.sources import apify
from news_edge.sources.underdog_backfill import BackfillConfig, Ledger, run, weekly_counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data/underdog"))
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument("--weeks", type=int, help="fetch at most this many weeks this run")
    parser.add_argument("--report", action="store_true", help="print weekly counts and exit")
    parser.add_argument(
        "--window",
        nargs=2,
        type=date.fromisoformat,
        metavar=("SINCE", "UNTIL"),
        help="fetch exactly this [SINCE, UNTIL) window, even if already fetched",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("backfill_underdog_posts")

    raw = yaml.safe_load(args.config.read_text()) or {}
    cfg = BackfillConfig.model_validate(raw.get("underdog_backfill", {}))
    today = datetime.now(tz=UTC).date()
    raw_dir = args.data_dir / "raw"

    with connect() as conn:
        if not args.report:
            token = get_settings().apify_token
            if token is None:
                log.error("APIFY_TOKEN is not set in .env")
                return 2
            raw_dir.mkdir(parents=True, exist_ok=True)
            with (raw_dir / ".backfill.lock").open("w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    log.warning("another post backfill is running; exiting")
                    return 0
                with apify.client(token) as http:
                    try:
                        summary = run(
                            http,
                            conn,
                            cfg,
                            raw_dir,
                            today,
                            max_weeks=args.weeks,
                            windows=[tuple(args.window)] if args.window else None,
                        )
                    except apify.ApifyError as exc:
                        log.error("%s", exc)
                        return 1
            log.info(
                "this run: %d weeks fetched, %d skipped, %d posts, $%.4f;"
                " ledger total $%.2f of $%.2f",
                len(summary.fetched),
                len(summary.skipped),
                summary.posts,
                summary.cost_usd,
                Ledger.load(raw_dir / "runs.jsonl").spent_usd,
                cfg.budget_usd,
            )
            for warning in summary.warnings:
                log.warning("%s", warning)
            if summary.stopped:
                log.warning("%s", summary.stopped)
        print(f"posts per week for @{cfg.account} (by t0, UTC):")
        for line in weekly_counts(conn, cfg.account, cfg.start, today):
            print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
