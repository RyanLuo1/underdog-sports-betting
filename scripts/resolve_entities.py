"""Resolve classified posts to Novig events and traded markets (fails closed).

Needs scripts/sync_novig_history.py and scripts/classify_posts.py to have run.

    uv run python scripts/resolve_entities.py
"""

import logging
import sys
from pathlib import Path

from news_edge.classify import rules
from news_edge.core.db import connect
from news_edge.entities.store import resolve_posts, sleeper_roster


def main() -> int:
    log_path = Path("logs/entity-resolution.log")
    log_path.parent.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, mode="w")],
    )
    roster = sleeper_roster(Path("data/reference/sleeper_players_nfl.json"))
    with connect() as conn:
        statuses, unmatched = resolve_posts(conn, rules.MODEL, rules.VERSION, "NFL", roster)
    print(f"{rules.MODEL} {rules.VERSION}: {sum(statuses.values())} subjects")
    for status, n in statuses.most_common():
        print(f"  {status:10s} {n}")
    print(f"unmatched names ({len(unmatched)} distinct; all logged to {log_path}):")
    for name, n in unmatched.most_common(25):
        print(f"  {n:3d}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
