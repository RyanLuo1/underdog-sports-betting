"""Back up finished days of the recorder's stream files, and report disk usage.

Each completed UTC day under data/novig/stream/<env>/<YYYY-MM-DD>/ becomes one archive,
<dest>/<env>/<YYYY-MM-DD>.tar.gz. An archive is written as .part, verified (it opens,
every member reads back and every gzipped member decompresses, and its file names,
sizes, and MD5s match the source), and only then renamed. What each verified archive holds is
recorded in a local manifest (<env>/backups.json), so later runs compare the source
against the manifest instead of reading archives back from iCloud, where macOS may
have removed the local copy. A day whose source changed, or that has no verified
archive, is (re)written, so a missed night is caught up by the next run. The gap log
(connections.jsonl) is copied beside the archives. Nothing local is deleted.
"""

import hashlib
import json
import logging
import re
import shutil
import subprocess
import tarfile
import zlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Literal

log = logging.getLogger(__name__)

_CHUNK = 1 << 20


class BackupError(RuntimeError):
    pass


@dataclass
class BackupSummary:
    written: list[str] = field(default_factory=list)
    present: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def completed_days(env_dir: Path, today: date) -> list[Path]:
    """Day folders strictly before today (UTC), oldest first."""
    days = []
    for path in env_dir.iterdir():
        try:
            day = date.fromisoformat(path.name)
        except ValueError:
            continue
        if path.is_dir() and day < today:
            days.append(path)
    return sorted(days)


def source_files(day_dir: Path) -> dict[str, int]:
    """name -> size for each file in a day folder, skipping partial writes."""
    return {
        p.name: p.stat().st_size
        for p in sorted(day_dir.iterdir())
        if p.is_file() and not p.name.endswith(".part")
    }


def write_archive(
    src_dir: Path, dest: Path, prefix: str, names: list[str] | None = None
) -> dict[str, int]:
    """Archive files from src_dir (all of them, or `names`) as <prefix>/<name>, verify
    the archive against the source, then rename it into place. Returns name -> size."""
    expected = source_files(src_dir)
    if names is not None:
        expected = {n: expected[n] for n in names}
    if not expected:
        raise BackupError(f"{src_dir} has no files")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    with tarfile.open(part, "w:gz", compresslevel=6) as tar:
        for name in expected:
            tar.add(src_dir / name, arcname=f"{prefix}/{name}", recursive=False)
    try:
        verify_archive(part, prefix, src_dir, list(expected))
    except Exception:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)
    return expected


def verify_archive(archive: Path, prefix: str, src_dir: Path, names: list[str]) -> None:
    """Raise BackupError unless the archive opens, every member reads back in full
    (gzipped members decompress cleanly), and its members are exactly `names`, each
    with the same size and MD5 as the file in src_dir."""
    found: dict[str, tuple[int, str]] = {}
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    raise BackupError(f"{archive.name}: unexpected member {member.name}")
                name = member.name.removeprefix(prefix + "/")
                f = tar.extractfile(member)
                if f is None:
                    raise BackupError(f"{archive.name}: cannot read {member.name}")
                with f:
                    read, md5 = _read_member(f, gzipped=name.endswith(".gz"))
                if read != member.size:
                    raise BackupError(f"{archive.name}: {name} read {read} of {member.size}")
                found[name] = (read, md5)
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise BackupError(f"{archive.name}: {type(exc).__name__}: {exc}") from exc
    missing = sorted(set(names) - found.keys())
    extra = sorted(found.keys() - set(names))
    if missing or extra:
        raise BackupError(
            f"{archive.name}: {len(found)} files vs {len(names)} in source"
            f" (missing {missing}, extra {extra})"
        )
    for name in names:
        path = src_dir / name
        if found[name] != (path.stat().st_size, file_md5(path)):
            raise BackupError(f"{archive.name}: {name} differs from the source")


def file_md5(path: Path) -> str:
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            md5.update(chunk)
    return md5.hexdigest()


def _read_member(f: IO[bytes], gzipped: bool) -> tuple[int, str]:
    """Read a member to the end and return its byte count and MD5. A gzipped member is
    also decompressed to the end, member by member, which checks every CRC."""
    count = 0
    md5 = hashlib.md5(usedforsecurity=False)
    inflater = zlib.decompressobj(31)
    mid_member = False
    while chunk := f.read(_CHUNK):
        count += len(chunk)
        md5.update(chunk)
        while gzipped and chunk:
            inflater.decompress(chunk)
            mid_member = True
            if inflater.eof:  # a member ended; another may follow (restarts append one)
                chunk = inflater.unused_data
                inflater = zlib.decompressobj(31)
                mid_member = False
            else:
                chunk = b""
    if mid_member:
        raise BackupError("gzip member is truncated")
    return count, md5.hexdigest()


def archive_present(dest: Path) -> bool:
    """True if the archive exists, including as an iCloud placeholder after macOS
    removed the local copy to save space (`.<name>.icloud`)."""
    return dest.exists() or dest.with_name(f".{dest.name}.icloud").exists()


def read_manifest(path: Path) -> dict[str, dict[str, int]]:
    if not path.exists():
        return {}
    data: dict[str, dict[str, int]] = json.loads(path.read_text())
    return data


def write_manifest(path: Path, manifest: dict[str, dict[str, int]]) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    tmp.replace(path)


def backup_env(env_dir: Path, dest_root: Path, today: date) -> BackupSummary:
    summary = BackupSummary()
    manifest_path = env_dir / "backups.json"
    manifest = read_manifest(manifest_path)
    for day_dir in completed_days(env_dir, today):
        label = f"{env_dir.name}/{day_dir.name}"
        dest = dest_root / env_dir.name / f"{day_dir.name}.tar.gz"
        try:
            files = source_files(day_dir)
            if manifest.get(day_dir.name) == files and archive_present(dest):
                summary.present.append(label)
                continue
            if day_dir.name in manifest:
                log.warning("%s: source changed since its backup; rewriting", label)
            manifest[day_dir.name] = write_archive(day_dir, dest, f"{env_dir.name}/{day_dir.name}")
            write_manifest(manifest_path, manifest)
            log.info("%s: backed up and verified (%.1f MB)", label, dest.stat().st_size / 1e6)
            summary.written.append(label)
        except (BackupError, OSError) as exc:
            log.error("%s: backup failed: %s", label, exc)
            summary.failed[label] = str(exc)
    gap_log = env_dir / "connections.jsonl"
    if gap_log.exists():
        try:
            copy = dest_root / env_dir.name / "connections.jsonl"
            copy.parent.mkdir(parents=True, exist_ok=True)
            tmp = copy.with_name(copy.name + ".part")
            shutil.copyfile(gap_log, tmp)
            tmp.replace(copy)
        except OSError as exc:
            log.error("%s: gap log copy failed: %s", env_dir.name, exc)
            summary.failed[f"{env_dir.name}/connections.jsonl"] = str(exc)
    return summary


def backup_raw(raw_dir: Path, dest_root: Path) -> BackupSummary:
    """Archive each raw trades CSV to <dest>/raw/<name>.tar.gz, verified like the stream
    days. Verified files are recorded in <raw_dir>/backups.json."""
    summary = BackupSummary()
    manifest_path = raw_dir / "backups.json"
    manifest = read_manifest(manifest_path)
    for csv in sorted(raw_dir.glob("trades-*.csv")):
        dest = raw_archive(dest_root, csv.name)
        try:
            if manifest.get(csv.name) == {csv.name: csv.stat().st_size} and archive_present(dest):
                summary.present.append(csv.name)
                continue
            manifest[csv.name] = write_archive(raw_dir, dest, "raw", [csv.name])
            write_manifest(manifest_path, manifest)
            log.info(
                "raw/%s: backed up and verified (%.1f MB)", csv.name, dest.stat().st_size / 1e6
            )
            summary.written.append(csv.name)
        except (BackupError, OSError) as exc:
            log.error("raw/%s: backup failed: %s", csv.name, exc)
            summary.failed[csv.name] = str(exc)
    return summary


def raw_archive(dest_root: Path, name: str) -> Path:
    return dest_root / "raw" / f"{name.removesuffix('.csv')}.tar.gz"


def deletable_raw(
    raw_dir: Path, dest_root: Path, parquet_dir: Path
) -> tuple[list[Path], list[str]]:
    """Raw CSVs safe to delete locally, and why each other one is not.

    Safe means: recorded as verified, its archive is on disk now (not only in iCloud) and
    verifies again against the local file (names, sizes, MD5), and its Parquet copy exists.
    """
    manifest = read_manifest(raw_dir / "backups.json")
    safe: list[Path] = []
    kept: list[str] = []
    for csv in sorted(raw_dir.glob("trades-*.csv")):
        dest = raw_archive(dest_root, csv.name)
        day = csv.name.removeprefix("trades-").removesuffix(".csv")
        if manifest.get(csv.name) != {csv.name: csv.stat().st_size}:
            kept.append(f"{csv.name}: no verified backup")
        elif not dest.exists():
            kept.append(f"{csv.name}: backup is not on this Mac (iCloud only); not re-checked")
        elif not (parquet_dir / f"trades-{day}.parquet").exists():
            kept.append(f"{csv.name}: no Parquet copy")
        else:
            try:
                verify_archive(dest, "raw", raw_dir, [csv.name])
            except BackupError as exc:
                kept.append(f"{csv.name}: backup did not verify: {exc}")
                continue
            safe.append(csv)
    return safe, kept


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


GIB = 1 << 30
DiskLevel = Literal["ok", "warning", "critical"]


@dataclass(frozen=True)
class DiskReport:
    line: str
    level: DiskLevel
    free_gib: float


def icloud_free_bytes() -> int | None:
    """Free iCloud storage, from `brctl quota` (no extra permissions). None if unknown."""
    try:
        out = subprocess.run(
            ["/usr/bin/brctl", "quota"], capture_output=True, text=True, timeout=30, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+) bytes of quota remaining", out)
    return int(match.group(1)) if match else None


def disk_report(
    repo_data: Path,
    stream_root: Path,
    backup_root: Path,
    warn_gib: float,
    critical_gib: float,
    icloud_free: int | None = None,
) -> DiskReport:
    usage = shutil.disk_usage(repo_data if repo_data.exists() else Path.home())
    free_gib = usage.free / GIB
    level: DiskLevel = (
        "critical" if free_gib < critical_gib else "warning" if free_gib < warn_gib else "ok"
    )
    parts = []
    if level == "critical":
        parts.append(f"CRITICAL: disk free below {critical_gib:g} GiB")
    elif level == "warning":
        parts.append(f"WARNING: disk free below {warn_gib:g} GiB")
    parts += [
        f"disk free {free_gib:.1f} GiB of {usage.total / GIB:.0f} GiB"
        f" ({100 * usage.used / usage.total:.0f}% used)",
        f"data/ {dir_size(repo_data) / GIB:.2f} GiB",
    ]
    if stream_root.exists():
        for env_dir in sorted(p for p in stream_root.iterdir() if p.is_dir()):
            parts.append(f"stream/{env_dir.name} {dir_size(env_dir) / 1e6:.0f} MB")
    parts.append(f"backups {dir_size(backup_root) / 1e6:.0f} MB")
    parts.append(
        f"iCloud free {icloud_free / GIB:.1f} GiB"
        if icloud_free is not None
        else "iCloud free unknown"
    )
    return DiskReport(line="; ".join(parts), level=level, free_gib=free_gib)


def today_utc() -> date:
    return datetime.now(tz=UTC).date()
