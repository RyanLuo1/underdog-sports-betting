"""Database connections."""

import psycopg
from sqlalchemy.engine import make_url

from news_edge.core.settings import get_settings


def libpq_url(database_url: str) -> str:
    """Turn a SQLAlchemy URL (postgresql+psycopg://...) into a plain libpq URL."""
    url = make_url(database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def connect(database_url: str | None = None) -> psycopg.Connection:
    """Open an autocommit connection; group statements with `conn.transaction()`."""
    url = database_url or get_settings().database_url
    return psycopg.connect(libpq_url(url), autocommit=True)
