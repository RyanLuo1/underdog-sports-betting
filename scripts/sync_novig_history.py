"""Sync Novig NFL events and markets (names and outcomes) from the history API.

Uses the read key in .env (NOVIG_TRADING_KEY_ID, NOVIG_PRIVATE_KEY_PATH), signed for
NOVIG_ENV. Rerunnable; rows are upserted.

    uv run python scripts/sync_novig_history.py --since 2026-06-01
"""

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from news_edge.core.db import connect
from news_edge.core.settings import get_settings
from news_edge.market.novig_auth import HOSTS, Signer
from news_edge.market.novig_history import sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--since", default="2026-06-01", help="oldest creation date to page back to"
    )
    parser.add_argument("--league", action="append", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    s = get_settings()
    if not s.novig_trading_key_id or not s.novig_private_key_path:
        print("set NOVIG_TRADING_KEY_ID and NOVIG_PRIVATE_KEY_PATH in .env", file=sys.stderr)
        return 2
    signer = Signer.from_pem(
        s.novig_trading_key_id.get_secret_value(), Path(s.novig_private_key_path).expanduser()
    )
    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
    with httpx.Client(base_url=HOSTS[s.novig_env], timeout=120) as http, connect() as conn:
        summary = sync(http, signer, conn, args.league or ["NFL"], since)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
