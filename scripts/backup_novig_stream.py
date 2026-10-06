"""Back up every finished UTC day of recorder stream files, then log disk usage.

launchd runs this at 03:00 local. Each day becomes a verified .tar.gz under the backup
folder in config/default.yaml (backup.dest); days already backed up are re-checked, so a
missed night is caught up. Local files are never deleted. Exits 1 if any day failed.

    uv run python scripts/backup_novig_stream.py
    uv run python scripts/backup_novig_stream.py --disk-only    # just the usage line
"""

import argparse
import fcntl
import logging
import sys
from pathlib import Path

import yaml

from news_edge.market.novig_stream_backup import (
    backup_env,
    disk_usage_line,
    today_utc,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument("--dest", type=Path, help="override backup.dest from the config")
    parser.add_argument("--disk-only", action="store_true", help="log disk usage and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("backup_novig_stream")

    config = yaml.safe_load(args.config.read_text()) or {}
    dest_root: Path = (args.dest or Path(config["backup"]["dest"])).expanduser()
    stream_root = args.data_dir / "novig" / "stream"

    failed = 0
    if not args.disk_only:
        stream_root.mkdir(parents=True, exist_ok=True)
        with (args.data_dir / "novig" / ".backup.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log.warning("another backup is running; exiting")
                return 0
            for env_dir in sorted(p for p in stream_root.iterdir() if p.is_dir()):
                summary = backup_env(env_dir, dest_root, today_utc())
                failed += len(summary.failed)
                log.info(
                    "%s: %d backed up, %d already present, %d failed -> %s",
                    env_dir.name,
                    len(summary.written),
                    len(summary.present),
                    len(summary.failed),
                    dest_root / env_dir.name,
                )
    log.info("%s", disk_usage_line(args.data_dir, stream_root, dest_root))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
