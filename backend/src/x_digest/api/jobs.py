from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from typing import Protocol

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from x_digest.ai.contracts import SummaryOutput
from x_digest.api.auth import require_internal_token
from x_digest.api.errors import APIError
from x_digest.api.schemas import PipelineResultResponse, SummaryResponse
from x_digest.models import Post, Summary, ThreadPost, XList
from x_digest.services.pipeline import PipelineResult
from x_digest.services.summarization import (
    DeterministicSummarizer,
    NormalizedContent,
    SummarizationService,
)

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
        request_fingerprint = _request_fingerprint(post_id)
        existing = self._session.scalar(
            select(Summary).where(Summary.regeneration_request_id == request_id)
        )
        if existing is not None:
            return _validate_idempotent_replay(existing, request_fingerprint)

        related = [
            summary
            for summary in self._session.scalars(select(Summary)).all()
            if post_id in summary.source_ids
        ]
        latest = max(
            related,
            key=lambda summary: (summary.created_at, summary.generation, summary.id),
            default=None,
        )
        content = self._normalized_unit(post, latest)
        output = await self._summarizer.summarize(content)
        self._validate_output(output, content.source_ids)
        summary = Summary(
            content_fingerprint=(
                latest.content_fingerprint
                if latest is not None
                else SummarizationService.fingerprint_for(content)
            ),
            model=self._summarizer.model_name,
            prompt_version="single-content-v1",
            generation=latest.generation + 1 if latest is not None else 1,
            regeneration_request_id=request_id,
            summary=output.summary,
            key_points=output.key_points,
            topics=output.topics,
            importance=output.importance,
            language=output.language,
            source_ids=output.source_ids,
            token_usage={"idempotency_request_fingerprint": request_fingerprint},
            status="succeeded",
        )
        self._session.add(summary)
        try:
            self._session.commit()
        except IntegrityError as error:
            self._session.rollback()
            winner = self._session.scalar(
                select(Summary).where(Summary.regeneration_request_id == request_id)
            )
            if winner is None:
                raise APIError(
                    409,
                    "summary_regeneration_conflict",
                    "Summary regeneration conflicted with another request",
                ) from error
            return _validate_idempotent_replay(winner, request_fingerprint)
        self._session.refresh(summary)
        return summary

    def _normalized_unit(self, post: Post, latest: Summary | None) -> NormalizedContent:
        ordered: Sequence[Post]
        if latest is not None:
            source_ids = tuple(latest.source_ids)
            posts = self._session.scalars(select(Post).where(Post.id.in_(source_ids))).all()
            by_id = {source.id: source for source in posts}
            if set(by_id) != set(source_ids):
                raise APIError(409, "summary_source_missing", "Summary source is unavailable")
            ordered = [by_id[source_id] for source_id in source_ids]
        else:
            thread_id = self._session.scalar(
                select(ThreadPost.thread_id)
                .where(ThreadPost.post_id == post.id)
                .order_by(ThreadPost.thread_id)
            )
            if thread_id is None:
                ordered = [post]
            else:
                ordered = self._session.scalars(
                    select(Post)
                    .join(ThreadPost, ThreadPost.post_id == Post.id)
                    .where(ThreadPost.thread_id == thread_id)
                    .order_by(ThreadPost.position, Post.id)
                ).all()
        return NormalizedContent(
            source_ids=tuple(source.id for source in ordered),
            text="\n\n".join(source.text for source in ordered),
        )

    @staticmethod
    def _validate_output(output: SummaryOutput, source_ids: tuple[str, ...]) -> None:
        if set(output.source_ids) != set(source_ids):
            raise APIError(502, "summary_invalid", "Summary provider returned invalid data")


def _request_fingerprint(post_id: str) -> str:
    payload = f"post-summary-regeneration\0{post_id}".encode()
    return hashlib.sha256(payload).hexdigest()


def _validate_idempotent_replay(summary: Summary, request_fingerprint: str) -> Summary:
    stored = (summary.token_usage or {}).get("idempotency_request_fingerprint")
    if stored != request_fingerprint:
        raise APIError(
            409,
            "idempotency_key_reused",
            "Idempotency key was already used for another request",
        )
    return summary


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
