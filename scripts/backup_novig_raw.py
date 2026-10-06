"""Back up Novig's raw trade CSVs (data/novig/raw/) to the backup folder, verified.

Each CSV becomes <backup.dest>/raw/trades-<date>.tar.gz, verified like the stream
backups (the archive opens, the file reads back, and its name, size, and MD5 match the
local CSV). Parquet copies in data/novig/parquet/ are never touched.

Nothing is deleted unless you ask:

    uv run python scripts/backup_novig_raw.py                   # back up and verify
    uv run python scripts/backup_novig_raw.py --delete-verified # then offer to delete

--delete-verified re-checks every backup against its local CSV before listing it, keeps
any CSV whose backup is only in iCloud (not on this Mac) or has no Parquet copy, and
asks for "yes" before deleting. The daily trades job never downloads a deleted CSV
again, because its Parquet copy stays.
"""

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path

import yaml

from news_edge.market.novig_stream_backup import backup_raw, deletable_raw


def main(argv: list[str] | None = None, ask: Callable[[str], str] = input) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument("--dest", type=Path, help="override backup.dest from the config")
    parser.add_argument(
        "--delete-verified",
        action="store_true",
        help="after backing up, list CSVs safe to delete and ask before deleting them",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("backup_novig_raw")

    config = yaml.safe_load(args.config.read_text()) or {}
    dest_root: Path = (args.dest or Path(config["backup"]["dest"])).expanduser()
    raw_dir = args.data_dir / "novig" / "raw"
    parquet_dir = args.data_dir / "novig" / "parquet"

    summary = backup_raw(raw_dir, dest_root)
    log.info(
        "raw: %d backed up, %d already present, %d failed -> %s",
        len(summary.written),
        len(summary.present),
        len(summary.failed),
        dest_root / "raw",
    )
    if summary.failed or not args.delete_verified:
        return 1 if summary.failed else 0

    safe, kept = deletable_raw(raw_dir, dest_root, parquet_dir)
    for reason in kept:
        print(f"keeping {reason}")
    if not safe:
        print("nothing is safe to delete")
        return 0
    size = sum(p.stat().st_size for p in safe)
    print(f"{len(safe)} raw CSVs ({size / 1e9:.2f} GB) are backed up, re-verified, and have")
    print(f"Parquet copies: {safe[0].name} .. {safe[-1].name}")
    if ask("Delete these local raw CSVs? Type yes to delete: ").strip() != "yes":
        print("nothing deleted")
        return 0
    for path in safe:
        path.unlink()
    log.info("deleted %d local raw CSVs (%.2f GB); backups and Parquet kept", len(safe), size / 1e9)
    return 0


if __name__ == "__main__":
    sys.exit(main())
