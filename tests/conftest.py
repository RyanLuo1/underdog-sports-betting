from collections.abc import Iterator

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from news_edge.core import settings
from news_edge.core.db import connect, libpq_url

TEST_DB = "news_edge_test"


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    """A fresh, migrated news_edge_test database. Skips if Postgres is not running."""
    main_url = settings.get_settings().database_url
    try:
        admin = psycopg.connect(libpq_url(main_url), autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres not running (docker compose up -d --wait db)")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")

    url = make_url(main_url).set(database=TEST_DB).render_as_string(hide_password=False)
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", url)
    settings.get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield url
    finally:
        mp.undo()
        settings.get_settings.cache_clear()


@pytest.fixture
def db(db_url: str) -> Iterator[psycopg.Connection]:
    with connect(db_url) as conn:
        conn.execute(
            "TRUNCATE novig_trades, novig_trade_files, entity_resolutions, classifications,"
            " posts, novig_markets, novig_events"
        )
        yield conn
