import asyncio

import httpx
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from x_digest.config import Settings
from x_digest.main import create_app
from x_digest.scheduler import build_production_pipeline
from x_digest.sources.x_api import XApiSource

FERNET_KEY = SecretStr("MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
INTERNAL_TOKEN = SecretStr("test-only-internal-api-token-0123456789abcdef")


def _settings() -> Settings:
    return Settings(
        internal_api_token=INTERNAL_TOKEN,
        x_client_id="test-x-client-id",
        x_oauth_redirect_uri="https://reader.test/api/x/callback",
        x_token_encryption_key=FERNET_KEY,
    )


def test_web_app_default_pipeline_uses_the_oauth_backed_x_source() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    api_client = httpx.AsyncClient(transport=transport)
    oauth_client = httpx.AsyncClient(transport=transport)
    app = create_app(
        settings=_settings(), x_api_http_client=api_client, oauth_http_client=oauth_client
    )

    source = app.state.pipeline_service._ingestion._source

    assert isinstance(source, XApiSource)
    assert source._token_service is app.state.x_oauth_client
    asyncio.run(api_client.aclose())
    asyncio.run(oauth_client.aclose())


def test_scheduler_production_pipeline_uses_the_oauth_backed_x_source() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    api_client = httpx.AsyncClient(transport=transport)
    oauth_http_client = httpx.AsyncClient(transport=transport)

    pipeline, oauth_client, source = build_production_pipeline(
        _settings(), factory, x_api_http_client=api_client, oauth_http_client=oauth_http_client
    )

    assert isinstance(pipeline._ingestion._source, XApiSource)
    assert source is pipeline._ingestion._source
    assert source._token_service is oauth_client
    asyncio.run(api_client.aclose())
    asyncio.run(oauth_http_client.aclose())
