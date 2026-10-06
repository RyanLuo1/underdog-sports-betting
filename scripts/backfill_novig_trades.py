"""Download Novig's public daily trades, convert them to Parquet, and load Postgres.

Safe to rerun: it only fetches, converts, or loads what is missing. The daily job
(scripts/launchd/) runs this same command.

    uv run python scripts/backfill_novig_trades.py              # everything Novig lists
    uv run python scripts/backfill_novig_trades.py --no-db      # files only
    uv run python scripts/backfill_novig_trades.py --since 2026-09-01
"""

import argparse
import fcntl
import logging
import sys
from datetime import date
from pathlib import Path

from news_edge.core.db import connect
from news_edge.market import novig_backfill


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data/novig"))
    parser.add_argument("--since", type=date.fromisoformat, help="first date, YYYY-MM-DD")
    parser.add_argument("--no-db", action="store_true", help="download and convert only")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="check local files against Novig's ETag (MD5) and re-fetch any that differ",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("backfill_novig_trades")

    args.data_dir.mkdir(parents=True, exist_ok=True)
    with (args.data_dir / ".backfill.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("another backfill is running; exiting")
            return 0
        conn = None if args.no_db else connect()
        try:
            summary = novig_backfill.run(args.data_dir, conn, since=args.since, verify=args.verify)
        finally:
            if conn is not None:
                conn.close()

    log.info(
        "done: %d downloaded (%d replaced), %d converted, %d loaded, %d failed",
        len(summary.downloaded),
        len(summary.replaced),
        len(summary.converted),
        len(summary.loaded),
        len(summary.failed),
    )
    for day, error in summary.failed.items():
        log.error("%s: %s", day, error)
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
