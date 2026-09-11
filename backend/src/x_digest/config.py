from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+pysqlite:///./x_digest.db"
    app_timezone: str = "UTC"
    internal_api_token: SecretStr | None = None
    x_bearer_token: SecretStr | None = None
    x_max_pages_per_sync: int = Field(default=5, ge=1, le=100)
    x_max_posts_per_sync: int = Field(default=500, ge=1, le=10_000)
    digest_cadence: Literal["6h", "daily", "both"] = "daily"
    llm_base_url: AnyHttpUrl | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_max_items_per_run: int = Field(default=100, ge=1, le=10_000)
