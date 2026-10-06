import gzip
import importlib.util
import tarfile
from datetime import date
from pathlib import Path

import pytest

from news_edge.market import novig_stream_backup as nb

TODAY = date(2026, 10, 8)
SCRIPT = Path(__file__).parents[2] / "scripts" / "backup_novig_stream.py"


def _day(env_dir: Path, day: str, hours: int = 3) -> Path:
    d = env_dir / day
    d.mkdir(parents=True)
    for h in range(hours):
        with gzip.open(d / f"{h:02d}.jsonl.gz", "wt") as f:
            f.write(f'{{"recv_ts":{h},"conn":"c","msg":{{}}}}\n' * 50)
    return d


@pytest.fixture
def env_dir(tmp_path: Path) -> Path:
    env = tmp_path / "stream" / "production"
    _day(env, "2026-10-06")
    _day(env, "2026-10-07", hours=2)
    _day(env, "2026-10-08")  # today: still being written
    (env / "connections.jsonl").write_text('{"ts":1,"kind":"start"}\n')
    return env


def test_backs_up_completed_days_only(env_dir: Path, tmp_path: Path) -> None:
    dest = tmp_path / "backup"
    summary = nb.backup_env(env_dir, dest, TODAY)
    assert summary.written == ["production/2026-10-06", "production/2026-10-07"]
    assert summary.failed == {}
    assert sorted(p.name for p in (dest / "production").iterdir()) == [
        "2026-10-06.tar.gz",
        "2026-10-07.tar.gz",
        "connections.jsonl",
    ]
    with tarfile.open(dest / "production" / "2026-10-07.tar.gz") as tar:
        assert tar.getnames() == [
            "production/2026-10-07/00.jsonl.gz",
            "production/2026-10-07/01.jsonl.gz",
        ]
    assert not list(dest.rglob("*.part"))


def test_rerun_is_a_noop_then_rebuilds_changed_day(env_dir: Path, tmp_path: Path) -> None:
    dest = tmp_path / "backup"
    nb.backup_env(env_dir, dest, TODAY)
    again = nb.backup_env(env_dir, dest, TODAY)
    assert again.written == []
    assert again.present == ["production/2026-10-06", "production/2026-10-07"]

    # A late hour file appears (an hour gzipped after the backup): the day is rebuilt.
    with gzip.open(env_dir / "2026-10-07" / "23.jsonl.gz", "wt") as f:
        f.write("{}\n")
    third = nb.backup_env(env_dir, dest, TODAY)
    assert third.written == ["production/2026-10-07"]
    with tarfile.open(dest / "production" / "2026-10-07.tar.gz") as tar:
        assert len(tar.getnames()) == 3


def test_icloud_placeholder_counts_as_present(env_dir: Path, tmp_path: Path) -> None:
    dest = tmp_path / "backup"
    nb.backup_env(env_dir, dest, TODAY)
    # macOS removed the local copy and left a placeholder; it is not read back.
    archive = dest / "production" / "2026-10-06.tar.gz"
    archive.rename(archive.with_name(".2026-10-06.tar.gz.icloud"))
    summary = nb.backup_env(env_dir, dest, TODAY)
    assert summary.present == ["production/2026-10-06", "production/2026-10-07"]
    assert summary.written == []


def test_unrecorded_archive_is_rewritten(env_dir: Path, tmp_path: Path) -> None:
    dest = tmp_path / "backup"
    (dest / "production").mkdir(parents=True)
    (dest / "production" / "2026-10-06.tar.gz").write_bytes(b"not verified")
    summary = nb.backup_env(env_dir, dest, TODAY)
    assert summary.written == ["production/2026-10-06", "production/2026-10-07"]
    with tarfile.open(dest / "production" / "2026-10-06.tar.gz") as tar:
        assert len(tar.getnames()) == 3


def test_missing_archive_is_rewritten(env_dir: Path, tmp_path: Path) -> None:
    dest = tmp_path / "backup"
    nb.backup_env(env_dir, dest, TODAY)
    (dest / "production" / "2026-10-07.tar.gz").unlink()
    summary = nb.backup_env(env_dir, dest, TODAY)
    assert summary.written == ["production/2026-10-07"]


def test_verify_rejects_count_mismatch(env_dir: Path, tmp_path: Path) -> None:
    day = env_dir / "2026-10-06"
    archive = tmp_path / "a.tar.gz"
    nb.write_archive(day, archive)
    expected = nb.source_files(day)
    expected["03.jsonl.gz"] = 10
    with pytest.raises(nb.BackupError, match="3 files vs 4 in source"):
        nb.verify_archive(archive, "production/2026-10-06", expected)


def test_verify_rejects_corrupt_archive(env_dir: Path, tmp_path: Path) -> None:
    day = env_dir / "2026-10-06"
    archive = tmp_path / "a.tar.gz"
    nb.write_archive(day, archive)
    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2])
    with pytest.raises(nb.BackupError):
        nb.verify_archive(archive, "production/2026-10-06", nb.source_files(day))


def test_verify_catches_truncated_hour_file(env_dir: Path, tmp_path: Path) -> None:
    day = env_dir / "2026-10-06"
    hour = day / "01.jsonl.gz"
    hour.write_bytes(hour.read_bytes()[:-12])  # cut the gzip trailer
    with pytest.raises(nb.BackupError, match=r"truncated|CRC|Error"):
        nb.write_archive(day, tmp_path / "a.tar.gz")
    assert not (tmp_path / "a.tar.gz").exists()
    assert not (tmp_path / "a.tar.gz.part").exists()


def test_verify_reads_multi_member_gzip(env_dir: Path, tmp_path: Path) -> None:
    hour = env_dir / "2026-10-06" / "00.jsonl.gz"
    with hour.open("ab") as f:
        f.write(gzip.compress(b"{}\n"))  # a restart appends a second member
    nb.write_archive(env_dir / "2026-10-06", tmp_path / "a.tar.gz")


def test_disk_usage_line(env_dir: Path, tmp_path: Path) -> None:
    line = nb.disk_usage_line(tmp_path, tmp_path / "stream", tmp_path / "none")
    assert line.startswith("disk free ")
    assert "stream/production" in line
    assert "backups 0 MB" in line


def test_script_backs_up_and_logs_usage(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    spec = importlib.util.spec_from_file_location("backup_novig_stream", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = tmp_path / "data"
    env = data / "novig" / "stream" / "production"
    _day(env, "2026-01-01")
    _day(env, "2026-01-02")
    dest = tmp_path / "backup"
    with caplog.at_level("INFO"):
        rc = module.main(["--data-dir", str(data), "--dest", str(dest)])
    assert rc == 0
    assert len(list((dest / "production").glob("*.tar.gz"))) == 2
    assert any("disk free" in r.message for r in caplog.records)
