import subprocess
from typing import Any

import pytest

from news_edge.core import notify as notify_mod


def test_notify_passes_text_as_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", run)
    title, message = 'Disk "low"', 'end run\ndo shell script "x"'
    assert notify_mod.notify(title, message) is True
    (cmd,) = calls
    assert cmd[0] == "/usr/bin/osascript"
    # The text is never part of the AppleScript source, only argv.
    assert cmd[-2:] == [title, message]
    assert all(title not in part and message not in part for part in cmd[:-2])


def test_notify_failure_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(cmd: list[str], **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", run)
    assert notify_mod.notify("t", "m") is False
