from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from x_digest.models import Link, Post, PostListMembership
from x_digest.services.link_content import LinkContentResult, LinkFetcher
from x_digest.services.link_extraction import extract_links


@dataclass(frozen=True)
class LinkProcessingResult:
    selected: int
    processed: int
    failed: int


class LinkProcessingService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        fetcher: LinkFetcher,
        max_attempts: int = 3,
        max_items: int = 20,
        retry_delay: timedelta = timedelta(minutes=5),
    ) -> None:
        self._session_factory = session_factory
        self._fetcher = fetcher
        self._max_attempts = max_attempts
        self._max_items = max_items
        self._retry_delay = retry_delay

    def process(self, list_id: str) -> LinkProcessingResult:
        selected = self._discover(list_id)
        processed = failed = 0
        for link_id in self._eligible_ids(list_id):
            with self._session_factory() as session:
                link = session.get(Link, link_id)
                if link is None:
                    continue
                url = link.canonical_url
            try:
                result = self._fetcher.fetch(url)
            except Exception:
                result = LinkContentResult(
                    status="failed", final_url=url, error_code="link_processing_failed"
                )
            with self._session_factory.begin() as session:
                link = session.get(Link, link_id)
                if link is None:
                    continue
                self._record_attempt(link, result)
            if result.status == "fetched":
                processed += 1
            else:
                failed += 1
        return LinkProcessingResult(selected=selected, processed=processed, failed=failed)

    def _discover(self, list_id: str) -> int:
        selected = 0
        with self._session_factory.begin() as session:
            posts = session.scalars(
                select(Post)
                .join(PostListMembership, PostListMembership.post_id == Post.id)
                .where(PostListMembership.list_id == list_id)
                .order_by(Post.created_at, Post.id)
            ).all()
            for post in posts:
                extracted_links = extract_links(
                    post.text, (post.entities_json or {}).get("urls", [])
                )
                selected += len(extracted_links)
                for extracted in extracted_links:
                    exists = session.scalar(
                        select(Link.id).where(
                            Link.post_id == post.id,
                            Link.canonical_url == extracted.canonical_url,
                        )
                    )
                    if exists is None:
                        session.add(
                            Link(
                                post_id=post.id,
                                original_url=extracted.original_url,
                                canonical_url=extracted.canonical_url,
                                fetch_status="pending",
                            )
                        )
        return selected

    def _eligible_ids(self, list_id: str) -> list[str]:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(Link.id)
                    .join(Post, Post.id == Link.post_id)
                    .join(PostListMembership, PostListMembership.post_id == Post.id)
                    .where(
                        PostListMembership.list_id == list_id,
                        Link.fetch_status.in_(("pending", "failed")),
                        Link.attempt_count < self._max_attempts,
                        or_(Link.next_attempt_at.is_(None), Link.next_attempt_at <= now),
                    )
                    .order_by(Link.id)
                    .limit(self._max_items)
                )
            )

    def _record_attempt(self, link: Link, result: LinkContentResult) -> None:
        link.attempt_count += 1
        link.final_url = result.final_url
        link.fetch_status = result.status
        link.title = result.title
        link.extracted_text = result.text
        link.error_code = result.error_code
        if result.status == "fetched":
            link.next_attempt_at = None
        elif link.attempt_count >= self._max_attempts:
            link.fetch_status = "exhausted"
            link.next_attempt_at = None
        else:
            multiplier = 2 ** (link.attempt_count - 1)
            link.next_attempt_at = datetime.now(UTC) + self._retry_delay * multiplier
