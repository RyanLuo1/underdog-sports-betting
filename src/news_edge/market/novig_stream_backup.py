"""Back up finished days of the recorder's stream files, and report disk usage.

Each completed UTC day under data/novig/stream/<env>/<YYYY-MM-DD>/ becomes one archive,
<dest>/<env>/<YYYY-MM-DD>.tar.gz. An archive is written as .part, verified (it opens,
every member reads back and every gzipped member decompresses, and its file names and
sizes match the source), and only then renamed. What each verified archive holds is
recorded in a local manifest (<env>/backups.json), so later runs compare the source
against the manifest instead of reading archives back from iCloud, where macOS may
have removed the local copy. A day whose source changed, or that has no verified
archive, is (re)written, so a missed night is caught up by the next run. The gap log
(connections.jsonl) is copied beside the archives. Nothing local is deleted.
"""

import json
import logging
import shutil
import tarfile
import zlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO

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


def write_archive(day_dir: Path, dest: Path) -> None:
    """Archive one day folder to dest, verify it, then rename it into place."""
    expected = source_files(day_dir)
    if not expected:
        raise BackupError(f"{day_dir} has no files")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    prefix = f"{day_dir.parent.name}/{day_dir.name}"
    with tarfile.open(part, "w:gz", compresslevel=6) as tar:
        for name in expected:
            tar.add(day_dir / name, arcname=f"{prefix}/{name}", recursive=False)
    try:
        verify_archive(part, prefix, expected)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)


def verify_archive(archive: Path, prefix: str, expected: dict[str, int]) -> None:
    """Raise BackupError unless the archive opens, every member reads back in full
    (gzipped members decompress cleanly), and names and sizes match `expected`."""
    found: dict[str, int] = {}
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
                    read = _read_member(f, gzipped=name.endswith(".gz"))
                if read != member.size:
                    raise BackupError(f"{archive.name}: {name} read {read} of {member.size}")
                found[name] = member.size
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise BackupError(f"{archive.name}: {type(exc).__name__}: {exc}") from exc
    if found != expected:
        missing = sorted(expected.keys() - found.keys())
        extra = sorted(found.keys() - expected.keys())
        changed = sorted(n for n in found.keys() & expected.keys() if found[n] != expected[n])
        raise BackupError(
            f"{archive.name}: {len(found)} files vs {len(expected)} in source"
            f" (missing {missing}, extra {extra}, size differs {changed})"
        )


def _read_member(f: IO[bytes], gzipped: bool) -> int:
    """Read a member to the end and return its byte count. A gzipped member is also
    decompressed to the end, member by member, which checks every CRC."""
    count = 0
    inflater = zlib.decompressobj(31)
    mid_member = False
    while chunk := f.read(_CHUNK):
        count += len(chunk)
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
    return count


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
            write_archive(day_dir, dest)
            manifest[day_dir.name] = files
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


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def disk_usage_line(repo_data: Path, stream_root: Path, backup_root: Path) -> str:
    usage = shutil.disk_usage(repo_data if repo_data.exists() else Path.home())
    gib = 1 << 30
    parts = [
        f"disk free {usage.free / gib:.1f} GiB of {usage.total / gib:.0f} GiB"
        f" ({100 * usage.used / usage.total:.0f}% used)",
        f"data/ {dir_size(repo_data) / gib:.2f} GiB",
    ]
    if stream_root.exists():
        for env_dir in sorted(p for p in stream_root.iterdir() if p.is_dir()):
            parts.append(f"stream/{env_dir.name} {dir_size(env_dir) / 1e6:.0f} MB")
    parts.append(f"backups {dir_size(backup_root) / 1e6:.0f} MB")
    return "; ".join(parts)


def today_utc() -> date:
    return datetime.now(tz=UTC).date()
