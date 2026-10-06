"""Runtime settings, read from the environment or a local .env file."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Blank lines copied from .env.example (KEY=) mean "unset", not "empty string".
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    database_url: str = "postgresql+psycopg://news_edge:news_edge@localhost:5432/news_edge"

    # Novig keys exist per environment; the key below must belong to novig_env.
    # The recorder uses a trading::read key (scripts/create_read_key.py).
    novig_env: Literal["paper", "production"] = "paper"
    novig_trading_key_id: SecretStr | None = None
    novig_private_key_path: str | None = None
    apify_token: SecretStr | None = None
    odds_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
