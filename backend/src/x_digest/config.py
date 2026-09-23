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
    x_client_id: str | None = None
    x_client_secret: SecretStr | None = None
    x_oauth_redirect_uri: AnyHttpUrl | None = None
    x_token_encryption_key: SecretStr | None = None
    x_max_pages_per_sync: int = Field(default=6, ge=1, le=100)
    x_max_posts_per_sync: int = Field(default=50, ge=1, le=10_000)
    x_page_size: int = Field(default=10, ge=1, le=100)
    x_ingestion_backfill_hours: int = Field(default=24, ge=1, le=24 * 30)
    x_ingestion_overlap_minutes: int = Field(default=10, ge=0, le=1440)
    link_max_attempts: int = Field(default=3, ge=1, le=20)
    link_max_items_per_run: int = Field(default=20, ge=1, le=500)
    link_retry_base_seconds: int = Field(default=300, ge=0, le=86_400)
    digest_cadence: Literal["6h", "daily", "both"] = "daily"
    llm_provider: Literal["deterministic", "openai", "gemini"] = "deterministic"
    llm_fallback_provider: Literal["none", "gemini"] = "none"
    llm_base_url: AnyHttpUrl = AnyHttpUrl("https://api.openai.com/v1")
    llm_api_key: SecretStr | None = None
    llm_model: str = "gpt-5.6-luna"
    llm_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "low"
    gemini_base_url: AnyHttpUrl = AnyHttpUrl(
        "https://generativelanguage.googleapis.com/v1"
    )
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_thinking_level: Literal["minimal", "low", "medium", "high"] = "low"
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    llm_max_output_tokens: int = Field(default=1200, ge=256, le=16_384)
    llm_max_items_per_run: int = Field(default=100, ge=1, le=10_000)
    notification_frontend_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:3000")
    notification_max_attempts: int = Field(default=3, ge=1, le=10)
    notification_retry_seconds: int = Field(default=300, ge=0, le=86_400)
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65_535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None
    smtp_starttls: bool = True
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
