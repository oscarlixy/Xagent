from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from x_digest.models import Link, Post, PostListMembership, Summary, SyncRun, Thread, ThreadPost
from x_digest.services.ingestion import IngestionService
from x_digest.services.link_extraction import extract_links
from x_digest.services.summarization import (
    NormalizedContent,
    SummarizationService,
    Summarizer,
)
from x_digest.services.threading import ThreadPostView, assemble_threads

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StageResult:
    counts: dict[str, int]
    duration_ms: int
    last_error: str | None = None


@dataclass(frozen=True)
class PipelineResult:
    run_id: str
    status: Literal["succeeded", "partial", "failed"]
    stages: dict[str, StageResult]


class PipelineService:
    """Offline list pipeline whose durable stages each own a transaction."""

    def __init__(
        self,
        *,
        ingestion: IngestionService,
        session_factory: sessionmaker[Session],
        summarizer: Summarizer,
        llm_max_items_per_run: int,
        link_processor: Callable[[Post], None] | None = None,
    ) -> None:
        self._ingestion = ingestion
        self._session_factory = session_factory
        self._summarizer = summarizer
        self._llm_max_items_per_run = llm_max_items_per_run
        self._link_processor = link_processor

    async def run_list_pipeline(self, list_id: str) -> PipelineResult:
        ingestion_started = time.monotonic()
        sync = await self._ingestion.sync_list(list_id)
        stages: dict[str, StageResult] = {
            "ingestion": StageResult(
                counts={
                    "pages_fetched": sync.pages_fetched,
                    "posts_seen": sync.posts_seen,
                    "posts_created": sync.posts_created,
                    "duplicates": sync.duplicates,
                    "rejected": sync.rejected,
                },
                duration_ms=_elapsed_ms(ingestion_started),
                last_error="ingestion_partial" if sync.status == "partial" else None,
            )
        }
        stages["processing"] = self._record_processing(list_id)
        stages["links"] = self._record_pending_links(list_id)
        stages["summary"] = await self._summarize_pending_posts(list_id)

        status: Literal["succeeded", "partial", "failed"] = sync.status
        if status == "succeeded" and any(stage.last_error for stage in stages.values()):
            status = "partial"
        self._finish_run(sync.run_id, status, stages)
        result = PipelineResult(run_id=sync.run_id, status=status, stages=stages)
        self._log_completion(list_id, result)
        return result

    def _record_pending_links(self, list_id: str) -> StageResult:
        started = time.monotonic()
        counts = {"selected": 0, "processed": 0, "failed": 0}
        last_error: str | None = None
        with self._session_factory.begin() as session:
            posts = session.scalars(
                select(Post)
                .join(PostListMembership, PostListMembership.post_id == Post.id)
                .where(PostListMembership.list_id == list_id)
                .order_by(Post.created_at, Post.id)
            ).all()
            for post in posts:
                entities = (post.entities_json or {}).get("urls", [])
                links = extract_links(post.text, entities)
                if not links:
                    continue
                counts["selected"] += len(links)
                for extracted in links:
                    link = session.scalar(
                        select(Link).where(
                            Link.post_id == post.id,
                            Link.canonical_url == extracted.canonical_url,
                        )
                    )
                    if link is None:
                        link = Link(
                            post_id=post.id,
                            original_url=extracted.original_url,
                            canonical_url=extracted.canonical_url,
                            fetch_status="pending",
                        )
                        session.add(link)
                        session.flush()
                    if link.fetch_status not in {"pending", "failed"}:
                        continue
                    try:
                        if self._link_processor is not None:
                            self._link_processor(post)
                        link.fetch_status = "pending"
                        link.error_code = None
                        counts["processed"] += 1
                    except Exception:  # link work must not block summarization
                        link.fetch_status = "failed"
                        link.error_code = "link_processing_failed"
                        counts["failed"] += 1
                        last_error = "link_processing_failed"
        return StageResult(counts=counts, duration_ms=_elapsed_ms(started), last_error=last_error)

    def _record_processing(self, list_id: str) -> StageResult:
        """Record deterministic local preparation before optional link work."""

        started = time.monotonic()
        counts = {"processed": 0, "threads_created": 0, "thread_posts_created": 0}
        with self._session_factory.begin() as session:
            posts = session.scalars(
                select(Post)
                .join(PostListMembership, PostListMembership.post_id == Post.id)
                .where(PostListMembership.list_id == list_id)
                .order_by(Post.created_at, Post.id)
            ).all()
            counts["processed"] = len(posts)
            by_id = {post.id: post for post in posts}
            groups = assemble_threads(
                [
                    ThreadPostView(
                        id=post.id,
                        author_id=post.author_id,
                        created_at=post.created_at,
                        conversation_id=post.conversation_id,
                        in_reply_to_post_id=post.in_reply_to_post_id,
                    )
                    for post in posts
                ]
            )
            for group in groups:
                first = group.posts[0]
                conversation_id = first.conversation_id or by_id[first.id].platform_post_id
                thread = session.scalar(
                    select(Thread).where(
                        Thread.conversation_id == conversation_id,
                        Thread.author_id == first.author_id,
                    )
                )
                if thread is None:
                    thread = Thread(
                        conversation_id=conversation_id,
                        author_id=first.author_id,
                        created_at=first.created_at,
                    )
                    session.add(thread)
                    session.flush()
                    counts["threads_created"] += 1
                existing = {
                    row.post_id: row
                    for row in session.scalars(
                        select(ThreadPost).where(ThreadPost.thread_id == thread.id)
                    )
                }
                for position, view in enumerate(group.posts):
                    thread_post = existing.get(view.id)
                    if thread_post is None:
                        session.add(
                            ThreadPost(
                                thread_id=thread.id,
                                post_id=by_id[view.id].id,
                                position=position,
                            )
                        )
                        counts["thread_posts_created"] += 1
                    else:
                        thread_post.position = position
        return StageResult(counts=counts, duration_ms=_elapsed_ms(started))

    async def _summarize_pending_posts(self, list_id: str) -> StageResult:
        started = time.monotonic()
        with self._session_factory() as session:
            threads = (
                session.scalars(
                    select(Thread)
                    .join(ThreadPost, ThreadPost.thread_id == Thread.id)
                    .join(Post, Post.id == ThreadPost.post_id)
                    .join(PostListMembership, PostListMembership.post_id == Post.id)
                    .where(PostListMembership.list_id == list_id)
                    .order_by(Thread.created_at, Thread.id)
                )
                .unique()
                .all()
            )
            pending = []
            for thread in threads:
                posts = session.scalars(
                    select(Post)
                    .join(ThreadPost, ThreadPost.post_id == Post.id)
                    .where(ThreadPost.thread_id == thread.id)
                    .order_by(ThreadPost.position, Post.id)
                ).all()
                content = NormalizedContent(
                    source_ids=tuple(post.id for post in posts),
                    text="\n\n".join(post.text for post in posts),
                )
                fingerprint = SummarizationService.fingerprint_for(content)
                current = session.scalar(
                    select(Summary).where(
                        Summary.content_fingerprint == fingerprint,
                        Summary.model == self._summarizer.model_name,
                        Summary.prompt_version == "single-content-v1",
                        Summary.generation == 1,
                        Summary.status == "succeeded",
                    )
                )
                if current is None:
                    pending.append(content)
            pending = pending[: self._llm_max_items_per_run]

        counts = {"selected": len(pending), "created": 0, "failed": 0}
        last_error: str | None = None
        for content in pending:
            try:
                # One summary per transaction makes provider failures retryable without data loss.
                with self._session_factory.begin() as session:
                    _, created = await SummarizationService(
                        session=session, summarizer=self._summarizer
                    ).summarize(content)
                    if created:
                        counts["created"] += 1
            except Exception:
                counts["failed"] += 1
                last_error = "summary_processing_failed"
        return StageResult(counts=counts, duration_ms=_elapsed_ms(started), last_error=last_error)

    def _finish_run(self, run_id: str, status: str, stages: dict[str, StageResult]) -> None:
        with self._session_factory.begin() as session:
            sync_run = session.get(SyncRun, run_id)
            if sync_run is None:  # pragma: no cover - ingestion creates the run
                return
            sync_run.status = status
            sync_run.finished_at = datetime.now().astimezone()
            sync_run.error_code = next(
                (stage.last_error for stage in stages.values() if stage.last_error), None
            )
            sync_run.debug_metadata = {
                "stages": {
                    name: {
                        "counts": stage.counts,
                        "duration_ms": stage.duration_ms,
                        "error_code": stage.last_error,
                    }
                    for name, stage in stages.items()
                }
            }

    @staticmethod
    def _log_completion(list_id: str, result: PipelineResult) -> None:
        logger.info(
            json.dumps(
                {
                    "event": "pipeline.completed",
                    "run_id": result.run_id,
                    "list_id": list_id,
                    "duration_ms": sum(stage.duration_ms for stage in result.stages.values()),
                    "counts": {name: stage.counts for name, stage in result.stages.items()},
                    "error_code": next(
                        (stage.last_error for stage in result.stages.values() if stage.last_error),
                        None,
                    ),
                }
            )
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
