import logging
from collections.abc import Awaitable, Callable
from secrets import token_hex

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class DatabaseUnavailableError(RuntimeError):
    """Raised when a readiness probe cannot reach the configured database."""


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message


def _request_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    return request_id if isinstance(request_id, str) else token_hex(16)


def _error_response(request: Request, *, status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message, "request_id": _request_id(request)},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def attach_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = token_hex(16)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, error: APIError) -> JSONResponse:
        return _error_response(
            request,
            status_code=error.status_code,
            code=error.code,
            message=error.message,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=422,
            code="validation_error",
            message="Request validation failed",
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, error: StarletteHTTPException) -> JSONResponse:
        if not request.url.path.startswith("/api/"):
            return JSONResponse(status_code=error.status_code, content={"detail": error.detail})
        code = "not_found" if error.status_code == 404 else "request_error"
        return _error_response(
            request,
            status_code=error.status_code,
            code=code,
            message="Resource not found" if error.status_code == 404 else "Request failed",
        )

    @app.exception_handler(DatabaseUnavailableError)
    async def database_unavailable_handler(
        request: Request, _error: DatabaseUnavailableError
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=503,
            code="database_unavailable",
            message="Database is unavailable",
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, _error: Exception) -> JSONResponse:
        logger.error(
            "Unexpected API error",
            exc_info=_error,
            extra={"request_id": _request_id(request)},
        )
        return _error_response(
            request,
            status_code=500,
            code="internal_error",
            message="Internal server error",
        )
