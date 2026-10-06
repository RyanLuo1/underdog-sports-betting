import importlib

import pytest

from news_edge.__main__ import main
from news_edge.core.settings import Settings

SUBPACKAGES = [
    "core",
    "sources",
    "classify",
    "entities",
    "market",
    "pricing",
    "signal",
    "execution",
    "ledger",
    "research",
    "ui",
]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackages_import(name: str) -> None:
    importlib.import_module(f"news_edge.{name}")


def test_cli_runs() -> None:
    assert main([]) == 0


def test_secrets_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APIFY_TOKEN", "super-secret")
    s = Settings(_env_file=None)
    assert s.apify_token is not None
    assert "super-secret" not in repr(s)
    assert s.apify_token.get_secret_value() == "super-secret"
