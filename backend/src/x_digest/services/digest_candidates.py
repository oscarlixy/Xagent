import hashlib
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from x_digest.models import Post, Summary, ThreadPost
from x_digest.services.digest_synthesis import DigestCandidate, DigestSource
from x_digest.services.digest_windows import DigestWindow


def load_digest_candidates(
    session: Session, window: DigestWindow
) -> tuple[DigestCandidate, ...]:
    units = _units_in_window(session, window)
    summaries = _latest_summaries_by_source_ids(session)
    return tuple(
        _candidate_from_unit(unit, summaries.get(frozenset(post.id for post in unit)))
        for unit in units
    )


def _units_in_window(session: Session, window: DigestWindow) -> tuple[tuple[Post, ...], ...]:
    posts = session.scalars(
        select(Post)
        .where(Post.created_at >= window.start, Post.created_at < window.end)
        .order_by(Post.created_at.desc(), Post.id.desc())
    ).all()
    seen: set[tuple[str, ...]] = set()
    units: list[tuple[Post, ...]] = []
    for post in posts:
        unit: tuple[Post, ...]
        membership = session.scalar(
            select(ThreadPost).where(ThreadPost.post_id == post.id).order_by(ThreadPost.id)
        )
        if membership is None:
            unit = (post,)
        else:
            unit = tuple(
                session.scalars(
                    select(Post)
                    .join(ThreadPost, ThreadPost.post_id == Post.id)
                    .where(ThreadPost.thread_id == membership.thread_id)
                    .order_by(ThreadPost.position, Post.id)
                ).all()
            )
        source_ids = tuple(item.id for item in unit)
        if source_ids in seen:
            continue
        seen.add(source_ids)
        units.append(unit)

    units.sort(
        key=lambda unit: max((post.created_at, post.id) for post in unit), reverse=True
    )
    return tuple(units)


def _latest_summaries_by_source_ids(
    session: Session,
) -> dict[frozenset[str], Summary]:
    summaries = session.scalars(
        select(Summary)
        .where(Summary.status == "succeeded")
        .order_by(Summary.created_at.desc(), Summary.generation.desc(), Summary.id.desc())
    ).all()
    latest: dict[frozenset[str], Summary] = {}
    for summary in summaries:
        latest.setdefault(frozenset(summary.source_ids), summary)
    return latest


def _candidate_from_unit(
    posts: Sequence[Post], summary: Summary | None
) -> DigestCandidate:
    source_ids = tuple(post.id for post in posts)
    sources = tuple(
        DigestSource(
            id=post.id,
            username=post.author.username,
            text=post.text,
            url=post.source_url,
        )
        for post in posts
    )
    if summary is not None:
        return DigestCandidate(
            id=f"summary:{summary.id}",
            summary_id=summary.id,
            source_ids=source_ids,
            summary=summary.summary,
            key_points=tuple(summary.key_points),
            topics=tuple(summary.topics),
            importance=summary.importance,
            language=summary.language,
            sources=sources,
        )

    fingerprint = hashlib.sha256("\n".join(source_ids).encode()).hexdigest()
    return DigestCandidate(
        id=f"raw:{fingerprint}",
        summary_id=None,
        source_ids=source_ids,
        summary="\n\n".join(post.text for post in posts),
        key_points=(),
        topics=("General",),
        importance=1,
        language="und",
        sources=sources,
    )
