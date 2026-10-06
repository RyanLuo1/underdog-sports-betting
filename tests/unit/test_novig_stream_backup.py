import gzip
import importlib.util
import shutil
import subprocess
import tarfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from news_edge.market import novig_stream_backup as nb

TODAY = date(2026, 10, 8)
SCRIPT = Path(__file__).parents[2] / "scripts" / "backup_novig_stream.py"
RAW_SCRIPT = Path(__file__).parents[2] / "scripts" / "backup_novig_raw.py"
PREFIX = "production/2026-10-06"


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
    nb.write_archive(day, archive, PREFIX)
    names = [*nb.source_files(day), "03.jsonl.gz"]
    with pytest.raises(nb.BackupError, match="3 files vs 4 in source"):
        nb.verify_archive(archive, PREFIX, day, names)


def test_verify_rejects_content_change(env_dir: Path, tmp_path: Path) -> None:
    day = env_dir / "2026-10-06"
    archive = tmp_path / "a.tar.gz"
    nb.write_archive(day, archive, PREFIX)
    hour = day / "00.jsonl.gz"
    data = bytearray(hour.read_bytes())
    data[20] ^= 0xFF  # same size, different bytes
    hour.write_bytes(bytes(data))
    with pytest.raises(nb.BackupError, match="differs from the source"):
        nb.verify_archive(archive, PREFIX, day, list(nb.source_files(day)))


def test_verify_rejects_corrupt_archive(env_dir: Path, tmp_path: Path) -> None:
    day = env_dir / "2026-10-06"
    archive = tmp_path / "a.tar.gz"
    nb.write_archive(day, archive, PREFIX)
    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2])
    with pytest.raises(nb.BackupError):
        nb.verify_archive(archive, PREFIX, day, list(nb.source_files(day)))


def test_verify_catches_truncated_hour_file(env_dir: Path, tmp_path: Path) -> None:
    day = env_dir / "2026-10-06"
    hour = day / "01.jsonl.gz"
    hour.write_bytes(hour.read_bytes()[:-12])  # cut the gzip trailer
    with pytest.raises(nb.BackupError, match=r"truncated|CRC|Error"):
        nb.write_archive(day, tmp_path / "a.tar.gz", PREFIX)
    assert not (tmp_path / "a.tar.gz").exists()
    assert not (tmp_path / "a.tar.gz.part").exists()


def test_verify_reads_multi_member_gzip(env_dir: Path, tmp_path: Path) -> None:
    hour = env_dir / "2026-10-06" / "00.jsonl.gz"
    with hour.open("ab") as f:
        f.write(gzip.compress(b"{}\n"))  # a restart appends a second member
    nb.write_archive(env_dir / "2026-10-06", tmp_path / "a.tar.gz", PREFIX)


def test_disk_report_levels(env_dir: Path, tmp_path: Path) -> None:
    free = shutil.disk_usage(tmp_path).free / nb.GIB
    ok = nb.disk_report(tmp_path, tmp_path / "stream", tmp_path / "none", 0, 0, 5 * nb.GIB)
    assert ok.level == "ok"
    assert ok.line.startswith("disk free ")
    assert "stream/production" in ok.line
    assert "backups 0 MB" in ok.line
    assert ok.line.endswith("iCloud free 5.0 GiB")
    warn = nb.disk_report(tmp_path, tmp_path, tmp_path, free + 1, 0)
    assert warn.level == "warning"
    assert warn.line.startswith("WARNING: disk free below")
    assert warn.line.endswith("iCloud free unknown")
    crit = nb.disk_report(tmp_path, tmp_path, tmp_path, free + 2, free + 1)
    assert crit.level == "critical"
    assert crit.line.startswith("CRITICAL: disk free below")


def test_icloud_free_parses_brctl(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 0, "48312285121 bytes of quota remaining in personal account\n", ""
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert nb.icloud_free_bytes() == 48312285121

    def fail(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("brctl")

    monkeypatch.setattr(subprocess, "run", fail)
    assert nb.icloud_free_bytes() is None


def test_script_backs_up_and_logs_usage(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("backup_novig_stream", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = tmp_path / "data"
    env = data / "novig" / "stream" / "production"
    _day(env, "2026-01-01")
    _day(env, "2026-01-02")
    dest = tmp_path / "backup"
    sent: list[str] = []

    def fake_notify(title: str, message: str) -> bool:
        sent.append(title)
        return True

    monkeypatch.setattr(module, "notify", fake_notify)
    monkeypatch.setattr(module, "icloud_free_bytes", lambda: 7 * nb.GIB)
    config = tmp_path / "config.yaml"
    # Thresholds above any real disk, so this run must warn and notify.
    config.write_text("backup:\n  dest: x\n  warn_free_gib: 1e9\n  critical_free_gib: 1\n")
    with caplog.at_level("INFO"):
        rc = module.main(["--data-dir", str(data), "--dest", str(dest), "--config", str(config)])
    assert rc == 0
    assert len(list((dest / "production").glob("*.tar.gz"))) == 2
    assert any("iCloud free 7.0 GiB" in r.message for r in caplog.records)
    assert sent and sent[0].startswith("Mac disk warning")
    assert any("disk free" in r.message for r in caplog.records)


# Raw trade CSVs


def _load(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def raw_data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    raw = data / "novig" / "raw"
    parquet = data / "novig" / "parquet"
    raw.mkdir(parents=True)
    parquet.mkdir(parents=True)
    for day in ("2026-08-04", "2026-08-05", "2026-08-06"):
        (raw / f"trades-{day}.csv").write_text(f"timestamp,side\n{day}T00:00:00Z,TAKER\n")
    for day in ("2026-08-04", "2026-08-05"):  # 08-06 has no Parquet copy
        (parquet / f"trades-{day}.parquet").write_bytes(b"PAR1")
    return data


def test_backup_raw_and_rerun(raw_data: Path, tmp_path: Path) -> None:
    raw = raw_data / "novig" / "raw"
    dest = tmp_path / "backup"
    first = nb.backup_raw(raw, dest)
    assert first.written == [
        "trades-2026-08-04.csv",
        "trades-2026-08-05.csv",
        "trades-2026-08-06.csv",
    ]
    with tarfile.open(dest / "raw" / "trades-2026-08-04.tar.gz") as tar:
        assert tar.getnames() == ["raw/trades-2026-08-04.csv"]
    assert nb.backup_raw(raw, dest).written == []


def test_deletable_raw_rules(raw_data: Path, tmp_path: Path) -> None:
    raw = raw_data / "novig" / "raw"
    parquet = raw_data / "novig" / "parquet"
    dest = tmp_path / "backup"
    nb.backup_raw(raw, dest)
    # 08-05's backup is only in iCloud now: it cannot be re-checked, so it is kept.
    archive = dest / "raw" / "trades-2026-08-05.tar.gz"
    archive.rename(archive.with_name(".trades-2026-08-05.tar.gz.icloud"))
    safe, kept = nb.deletable_raw(raw, dest, parquet)
    assert [p.name for p in safe] == ["trades-2026-08-04.csv"]
    assert kept == [
        "trades-2026-08-05.csv: backup is not on this Mac (iCloud only); not re-checked",
        "trades-2026-08-06.csv: no Parquet copy",
    ]


def test_deletable_raw_rejects_changed_backup(raw_data: Path, tmp_path: Path) -> None:
    raw = raw_data / "novig" / "raw"
    dest = tmp_path / "backup"
    nb.backup_raw(raw, dest)
    other = tmp_path / "other"
    other.mkdir()
    (other / "trades-2026-08-04.csv").write_text("timestamp,side\nX,MAKER\n")
    nb.write_archive(other, dest / "raw" / "trades-2026-08-04.tar.gz", "raw")
    safe, kept = nb.deletable_raw(raw, dest, raw_data / "novig" / "parquet")
    assert [p.name for p in safe] == ["trades-2026-08-05.csv"]
    assert kept[0].startswith("trades-2026-08-04.csv: backup did not verify")


def test_raw_script_deletes_only_after_yes(raw_data: Path, tmp_path: Path) -> None:
    module = _load(RAW_SCRIPT)
    raw = raw_data / "novig" / "raw"
    args = ["--data-dir", str(raw_data), "--dest", str(tmp_path / "backup")]
    config = tmp_path / "config.yaml"
    config.write_text("backup:\n  dest: x\n")
    args += ["--config", str(config)]

    assert module.main(args) == 0  # backup only: nothing deleted, no prompt
    assert len(list(raw.glob("*.csv"))) == 3

    prompts: list[str] = []

    def no(prompt: str) -> str:
        prompts.append(prompt)
        return "y"

    assert module.main([*args, "--delete-verified"], ask=no) == 0
    assert len(prompts) == 1
    assert len(list(raw.glob("*.csv"))) == 3

    assert module.main([*args, "--delete-verified"], ask=lambda _: "yes") == 0
    left = sorted(p.name for p in raw.glob("*.csv"))
    assert left == ["trades-2026-08-06.csv"]  # no Parquet copy, so kept
    assert len(list((raw_data / "novig" / "parquet").iterdir())) == 2
