from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from x_digest.api.auth import require_internal_token
from x_digest.api.errors import APIError
from x_digest.services.oauth_client import OAuthClientError

router = APIRouter(prefix="/api/x/oauth", dependencies=[Depends(require_internal_token)])


class OAuthCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(min_length=1)
    verifier: str = Field(min_length=1)


@router.post("/callback", status_code=status.HTTP_204_NO_CONTENT)
async def oauth_callback(payload: OAuthCallbackRequest, request: Request) -> Response:
    try:
        await request.app.state.x_oauth_client.exchange_code(
            code=payload.code,
            verifier=payload.verifier,
        )
    except OAuthClientError as error:
        raise APIError(error.status_code, error.code, error.message) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
