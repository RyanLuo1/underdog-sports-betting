"""Runtime settings, read from the environment or a local .env file."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://news_edge:news_edge@localhost:5432/news_edge"

    novig_trading_key_id: SecretStr | None = None
    novig_private_key_path: str | None = None
    apify_token: SecretStr | None = None
    jev_api_key: SecretStr | None = None
    odds_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
