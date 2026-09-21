from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from x_digest.api.auth import require_internal_token
from x_digest.api.errors import APIError
from x_digest.api.schemas import DigestItemResponse, DigestResponse
from x_digest.models import Digest
from x_digest.services.digest_synthesis import DigestSynthesizer
from x_digest.services.digests import DigestWindow, generate_digest

router = APIRouter(prefix="/api/digests", dependencies=[Depends(require_internal_token)])


def _session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


class DigestService:
    def __init__(
        self,
        session: Session,
        digest_synthesizer: DigestSynthesizer | None = None,
    ) -> None:
        self._session = session
        self._digest_synthesizer = digest_synthesizer

    def list(self) -> list[DigestResponse]:
        digests = self._session.scalars(
            select(Digest)
            .options(selectinload(Digest.items))
            .order_by(Digest.created_at.desc(), Digest.version.desc(), Digest.id.desc())
        ).all()
        return [_response(digest) for digest in digests]

    def get(self, digest_id: str) -> DigestResponse:
        digest = self._session.scalar(
            select(Digest).options(selectinload(Digest.items)).where(Digest.id == digest_id)
        )
        if digest is None:
            raise APIError(404, "digest_not_found", "Digest not found")
        return _response(digest)

    async def regenerate(self, digest_id: str) -> DigestResponse:
        existing = self._session.get(Digest, digest_id)
        if existing is None:
            raise APIError(404, "digest_not_found", "Digest not found")
        if self._digest_synthesizer is None:
            raise RuntimeError("digest_synthesizer_not_configured")
        window = None
        if existing.window_start_at is not None and existing.window_end_at is not None:
            window = DigestWindow(
                start=existing.window_start_at,
                end=existing.window_end_at,
                cadence="6h" if existing.window_key.endswith("/6h") else "daily",
                timezone=existing.timezone,
            )
        digest, _ = await generate_digest(
            self._session,
            synthesizer=self._digest_synthesizer,
            window=window,
            window_key=None if window is not None else existing.window_key,
            regenerate=True,
        )
        self._session.commit()
        return self.get(digest.id)


def _response(digest: Digest) -> DigestResponse:
    return DigestResponse(
        id=digest.id,
        window_key=digest.window_key,
        version=digest.version,
        timezone=digest.timezone,
        rendered_content=digest.rendered_content,
        status=digest.status,
        created_at=digest.created_at,
        items=[
            DigestItemResponse(
                id=item.id,
                source_type=item.source_type,
                source_id=item.source_id,
                topic=item.topic,
                position=item.position,
                snapshot=item.snapshot_json,
            )
            for item in sorted(digest.items, key=lambda item: (item.position, item.id))
        ],
    )


@router.get("", response_model=list[DigestResponse])
def list_digests(session: Session = Depends(_session)) -> list[DigestResponse]:
    return DigestService(session).list()


@router.get("/{digest_id}", response_model=DigestResponse)
def get_digest(digest_id: str, session: Session = Depends(_session)) -> DigestResponse:
    return DigestService(session).get(digest_id)


@router.post(
    "/{digest_id}/regenerate",
    response_model=DigestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def regenerate_digest(
    digest_id: str,
    request: Request,
    session: Session = Depends(_session),
) -> DigestResponse:
    return await DigestService(
        session,
        digest_synthesizer=request.app.state.digest_synthesizer,
    ).regenerate(digest_id)
