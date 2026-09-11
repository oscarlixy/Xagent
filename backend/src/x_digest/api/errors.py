from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class DatabaseUnavailableError(RuntimeError):
    """Raised when a readiness probe cannot reach the configured database."""


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DatabaseUnavailableError)
    async def database_unavailable_handler(
        _request: Request, _error: DatabaseUnavailableError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"code": "database_unavailable", "message": "Database is unavailable"},
        )
