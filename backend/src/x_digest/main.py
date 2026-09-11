from fastapi import FastAPI
from sqlalchemy import create_engine, text

from x_digest.api.errors import DatabaseUnavailableError, install_error_handlers
from x_digest.config import Settings


def create_app(*, database_url: str | None = None) -> FastAPI:
    """Create the web application without initializing external providers."""

    settings = Settings()
    if database_url is not None:
        settings = settings.model_copy(update={"database_url": database_url})

    app = FastAPI(title="X Digest")
    app.state.settings = settings
    install_error_handlers(app)

    @app.get("/health/live")
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness() -> dict[str, str]:
        try:
            engine = create_engine(settings.database_url)
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            engine.dispose()
        except Exception as error:  # pragma: no cover - error type differs by driver
            raise DatabaseUnavailableError from error
        return {"status": "ok"}

    return app


app = create_app()
