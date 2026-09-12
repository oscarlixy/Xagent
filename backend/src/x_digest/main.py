from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from x_digest.api.auth import validate_internal_api_token
from x_digest.api.digests import router as digests_router
from x_digest.api.errors import DatabaseUnavailableError, install_error_handlers
from x_digest.api.jobs import PipelineRunner
from x_digest.api.jobs import router as jobs_router
from x_digest.api.lists import router as lists_router
from x_digest.api.posts import router as posts_router
from x_digest.api.status import router as status_router
from x_digest.api.x_oauth import router as x_oauth_router
from x_digest.config import Settings
from x_digest.services.ingestion import IngestionService
from x_digest.services.oauth_client import XOAuthClient
from x_digest.services.pipeline import PipelineService
from x_digest.services.summarization import DeterministicSummarizer
from x_digest.sources.fake import FakeXSource


def create_app(
    *,
    database_url: str | None = None,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    pipeline_service: PipelineRunner | None = None,
    oauth_http_client: httpx.AsyncClient | None = None,
    oauth_clock: Callable[[], datetime] | None = None,
    _validate_token_on_create: bool = True,
) -> FastAPI:
    """Create the web application without initializing external providers."""

    settings = settings or Settings()
    if database_url is not None:
        settings = settings.model_copy(update={"database_url": database_url})
    if _validate_token_on_create:
        validate_internal_api_token(settings)

    database_initialization_failed = False
    if session_factory is None:
        try:
            engine = create_engine(settings.database_url)
            session_factory = sessionmaker(engine, expire_on_commit=False)
        except Exception:  # pragma: no cover - driver loading differs by deployment
            database_initialization_failed = True
            session_factory = sessionmaker(expire_on_commit=False)
    summarizer = DeterministicSummarizer()
    if pipeline_service is None:
        pipeline_service = PipelineService(
            ingestion=IngestionService(
                session_factory=session_factory,
                source=FakeXSource({}),
                max_pages=settings.x_max_pages_per_sync,
                max_posts=settings.x_max_posts_per_sync,
            ),
            session_factory=session_factory,
            summarizer=summarizer,
            llm_max_items_per_run=settings.llm_max_items_per_run,
        )
    x_oauth_client = XOAuthClient(
        session_factory=session_factory,
        client_id=settings.x_client_id,
        redirect_uri=settings.x_oauth_redirect_uri,
        encryption_key=settings.x_token_encryption_key,
        http_client=oauth_http_client,
        clock=oauth_clock,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        validate_internal_api_token(settings)
        try:
            yield
        finally:
            await x_oauth_client.aclose()

    app = FastAPI(title="X Digest", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.pipeline_service = pipeline_service
    app.state.summarizer = summarizer
    app.state.x_oauth_client = x_oauth_client
    install_error_handlers(app)

    @app.get("/health/live")
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness() -> dict[str, str]:
        try:
            if database_initialization_failed:
                raise DatabaseUnavailableError
            with session_factory() as session:
                session.execute(text("SELECT 1"))
        except DatabaseUnavailableError:
            raise
        except Exception as error:  # pragma: no cover - error type differs by driver
            raise DatabaseUnavailableError from error
        return {"status": "ok"}

    app.include_router(lists_router)
    app.include_router(posts_router)
    app.include_router(digests_router)
    app.include_router(jobs_router)
    app.include_router(status_router)
    app.include_router(x_oauth_router)
    return app


app = create_app(_validate_token_on_create=False)
