"""Record Novig's order-book stream until stopped. launchd runs this under caffeinate.

Reads NOVIG_ENV, NOVIG_TRADING_KEY_ID, and NOVIG_PRIVATE_KEY_PATH from .env (a
trading::read key from scripts/create_read_key.py). Exits 0 without connecting when the
key is missing or another recorder holds the lock, so launchd does not respin it; exits
non-zero on a crash, which launchd restarts.

    uv run python scripts/run_novig_recorder.py
    uv run python scripts/run_novig_recorder.py --gaps    # print missing windows and exit
"""

import argparse
import asyncio
import fcntl
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from news_edge.core.settings import get_settings
from news_edge.market.novig_auth import HOSTS, Signer
from news_edge.market.novig_stream import (
    WS_PATH,
    Recorder,
    RecorderConfig,
    fetch_catalog,
    missing_windows,
    read_gap_log,
)

log = logging.getLogger("run_novig_recorder")


def _fmt(ms: int | None) -> str:
    if ms is None:
        return "now (open)"
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="milliseconds")


def print_gaps(root: Path) -> int:
    path = root / "connections.jsonl"
    if not path.exists():
        print(f"no gap log at {path}")
        return 0
    for w in missing_windows(read_gap_log(path)):
        if w.scope == "all":
            where = "all events"
        elif w.scope == "connection":
            where = f"connection {w.slot}: events {', '.join(w.events or []) or '(none)'}"
        else:
            where = f"connection {w.slot}: market {w.market}"
        print(f"{_fmt(w.start)} -> {_fmt(w.end)}  {where}  ({w.cause})")
    return 0


async def record(root: Path, cfg: RecorderConfig, signer: Signer, host: str) -> None:
    async with httpx.AsyncClient(base_url=host, timeout=30) as http:
        recorder = Recorder(
            ws_url=host.replace("https://", "wss://", 1) + WS_PATH,
            headers=lambda: signer.headers("GET", WS_PATH),
            catalog=lambda: fetch_catalog(http, cfg.leagues),
            root=root,
            cfg=cfg,
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, recorder.stop)
        await recorder.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data/novig"))
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument("--gaps", action="store_true", help="print missing windows and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    root = args.data_dir / "stream" / settings.novig_env
    if args.gaps:
        return print_gaps(root)

    if not settings.novig_trading_key_id or not settings.novig_private_key_path:
        log.error("set NOVIG_TRADING_KEY_ID and NOVIG_PRIVATE_KEY_PATH in .env; not starting")
        return 0
    signer = Signer.from_pem(
        settings.novig_trading_key_id.get_secret_value(),
        Path(settings.novig_private_key_path).expanduser(),
    )
    raw = yaml.safe_load(args.config.read_text()) or {}
    cfg = RecorderConfig.model_validate(raw.get("recorder", {}))

    args.data_dir.mkdir(parents=True, exist_ok=True)
    with (args.data_dir / ".recorder.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("another recorder is running; exiting")
            return 0
        log.info("recording %s %s to %s", settings.novig_env, cfg.leagues, root)
        asyncio.run(record(root, cfg, signer, HOSTS[settings.novig_env]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
