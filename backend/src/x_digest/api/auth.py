from __future__ import annotations

import hmac

from fastapi import Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from x_digest.api.errors import APIError
from x_digest.config import Settings

_bearer = HTTPBearer(auto_error=False)


def validate_internal_api_token(settings: Settings) -> str:
    configured = settings.internal_api_token
    token = configured.get_secret_value() if configured is not None else ""
    if len(token.encode("utf-8")) < 32:
        raise RuntimeError("INTERNAL_API_TOKEN must contain at least 32 bytes")
    return token


def require_internal_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    expected = validate_internal_api_token(request.app.state.settings)
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise APIError(401, "unauthorized", "Authentication required")
    if not hmac.compare_digest(credentials.credentials.encode(), expected.encode()):
        raise APIError(401, "unauthorized", "Authentication required")
