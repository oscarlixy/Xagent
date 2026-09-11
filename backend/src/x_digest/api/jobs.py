from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from x_digest.ai.contracts import SummaryOutput
from x_digest.api.auth import require_internal_token
from x_digest.api.errors import APIError
from x_digest.api.schemas import PipelineResultResponse, SummaryResponse
from x_digest.models import Post, Summary, XList
from x_digest.services.pipeline import PipelineResult
from x_digest.services.summarization import DeterministicSummarizer, NormalizedContent

router = APIRouter(prefix="/api", dependencies=[Depends(require_internal_token)])


class PipelineRunner(Protocol):
    async def run_list_pipeline(self, list_id: str) -> PipelineResult: ...


def _session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


class JobService:
    def __init__(
        self,
        *,
        session: Session,
        pipeline: PipelineRunner,
        summarizer: DeterministicSummarizer,
    ) -> None:
        self._session = session
        self._pipeline = pipeline
        self._summarizer = summarizer

    async def sync(self, list_id: str) -> PipelineResult:
        if self._session.get(XList, list_id) is None:
            raise APIError(404, "list_not_found", "List not found")
        return await self._pipeline.run_list_pipeline(list_id)

    async def regenerate_summary(self, post_id: str, request_id: str) -> Summary:
        post = self._session.get(Post, post_id)
        if post is None:
            raise APIError(404, "post_not_found", "Post not found")
        existing = self._session.scalar(
            select(Summary).where(Summary.regeneration_request_id == request_id)
        )
        if existing is not None:
            return existing

        related = [
            summary
            for summary in self._session.scalars(select(Summary)).all()
            if post_id in summary.source_ids
        ]
        latest = max(related, key=lambda summary: summary.generation, default=None)
        content = NormalizedContent(source_ids=(post.id,), text=post.text)
        output = await self._summarizer.summarize(content)
        self._validate_output(output, post.id)
        summary = Summary(
            content_fingerprint=(
                latest.content_fingerprint if latest is not None else f"manual:{post.id}"
            ),
            model=self._summarizer.model_name,
            prompt_version="single-content-v1",
            generation=(latest.generation + 1 if latest is not None else 1),
            regeneration_request_id=request_id,
            summary=output.summary,
            key_points=output.key_points,
            topics=output.topics,
            importance=output.importance,
            language=output.language,
            source_ids=output.source_ids,
            status="succeeded",
        )
        self._session.add(summary)
        self._session.commit()
        self._session.refresh(summary)
        return summary

    @staticmethod
    def _validate_output(output: SummaryOutput, post_id: str) -> None:
        if set(output.source_ids) != {post_id}:
            raise APIError(502, "summary_invalid", "Summary provider returned invalid data")


@router.post("/jobs/sync/{list_id}", response_model=PipelineResultResponse)
async def sync_list(
    list_id: str, request: Request, session: Session = Depends(_session)
) -> PipelineResult:
    service = JobService(
        session=session,
        pipeline=request.app.state.pipeline_service,
        summarizer=request.app.state.summarizer,
    )
    return await service.sync(list_id)


@router.post(
    "/posts/{post_id}/summaries/regenerate",
    response_model=SummaryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def regenerate_summary(
    post_id: str,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=255, alias="Idempotency-Key"),
    session: Session = Depends(_session),
) -> Summary:
    service = JobService(
        session=session,
        pipeline=request.app.state.pipeline_service,
        summarizer=request.app.state.summarizer,
    )
    return await service.regenerate_summary(post_id, idempotency_key)
