from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from x_digest.api.auth import require_internal_token
from x_digest.api.schemas import StatusResponse
from x_digest.observability import status_snapshot

router = APIRouter(prefix="/api/status", dependencies=[Depends(require_internal_token)])


def _session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


@router.get("", response_model=StatusResponse)
def get_status(session: Session = Depends(_session)) -> dict[str, object]:
    return status_snapshot(session)
