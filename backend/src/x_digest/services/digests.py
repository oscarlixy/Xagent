import re
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from x_digest.ai.contracts import DigestOutput
from x_digest.ai.openai import LLMProviderError
from x_digest.models import Digest, DigestItem, Post, Summary
from x_digest.repositories.transactions import ensure_sqlite_outer_transaction
from x_digest.services.digest_candidates import load_digest_candidates
from x_digest.services.digest_synthesis import (
    DigestCandidate,
    DigestSynthesizer,
    DigestValidationError,
    fallback_digest_output,
    validate_digest_output,
)
from x_digest.services.digest_windows import DigestWindow as DigestWindow
from x_digest.services.digest_windows import window_for as window_for


async def generate_digest(
    session: Session,
    *,
    synthesizer: DigestSynthesizer,
    window: DigestWindow | None = None,
    window_key: str | None = None,
    regenerate: bool = False,
) -> tuple[Digest, bool]:
    if window is None and window_key is None:
        raise ValueError("window or window_key is required")
    key = window.key if window is not None else window_key
    assert key is not None
    latest = _latest_digest(session, key)
    if latest is not None and not regenerate:
        return latest, False

    if window is None:
        candidates: tuple[DigestCandidate, ...] = ()
        output = fallback_digest_output(candidates)
    else:
        candidates = load_digest_candidates(session, window)
        try:
            result = await synthesizer.synthesize_digest(candidates)
            validate_digest_output(result.output, candidates)
            output = result.output
        except (LLMProviderError, DigestValidationError):
            output = fallback_digest_output(candidates)

    return _persist_event_digest(
        session,
        window=window,
        window_key=key,
        output=output,
        candidates=candidates,
    )


def _persist_event_digest(
    session: Session,
    *,
    window: DigestWindow | None,
    window_key: str,
    output: DigestOutput,
    candidates: tuple[DigestCandidate, ...],
) -> tuple[Digest, bool]:
    source_by_id = {
        source.id: source for candidate in candidates for source in candidate.sources
    }
    for _attempt in range(20):
        latest = _latest_digest(session, window_key)
        digest = Digest(
            window_key=window_key,
            version=1 if latest is None else latest.version + 1,
            timezone=(
                window.timezone
                if window is not None
                else _timezone_from_legacy_key(window_key)
            ),
            window_start_at=window.start if window is not None else None,
            window_end_at=window.end if window is not None else None,
        )
        ensure_sqlite_outer_transaction(session)
        try:
            with session.begin_nested():
                session.add(digest)
                session.flush()
                rendered_topics: list[dict[str, object]] = []
                position = 0
                for topic_index, topic in enumerate(output.topics):
                    rendered_events: list[dict[str, object]] = []
                    for event_index, event in enumerate(topic.events):
                        snapshot: dict[str, object] = {
                            "title": event.title,
                            "what_happened": event.what_happened,
                            "why_it_matters": event.why_it_matters,
                            "event_type": event.event_type,
                            "importance": event.importance,
                            "source_ids": list(event.source_ids),
                            "sources": [
                                {
                                    "id": source_by_id[source_id].id,
                                    "username": source_by_id[source_id].username,
                                    "text": source_by_id[source_id].text,
                                    "url": source_by_id[source_id].url,
                                }
                                for source_id in event.source_ids
                            ],
                        }
                        identity = ":".join(
                            [
                                digest.id,
                                str(topic_index),
                                str(event_index),
                                *event.source_ids,
                            ]
                        )
                        item = DigestItem(
                            digest_id=digest.id,
                            source_type="event",
                            source_id=str(uuid5(NAMESPACE_URL, identity)),
                            topic=topic.name,
                            position=position,
                            snapshot_json=snapshot,
                        )
                        session.add(item)
                        session.flush()
                        rendered_events.append({**snapshot, "item_id": item.id})
                        position += 1
                    rendered_topics.append(
                        {"name": topic.name, "events": rendered_events}
                    )
                digest.rendered_content = {
                    "schema_version": "event-digest-v1",
                    "title": _title(window, window_key),
                    "overview": output.overview,
                    "topics": rendered_topics,
                    "excluded_source_ids": list(output.excluded_source_ids),
                }
                session.flush()
        except IntegrityError:
            continue
        return digest, True
    raise RuntimeError("digest_version_conflict")


def _latest_digest(session: Session, window_key: str) -> Digest | None:
    return session.scalar(
        select(Digest)
        .where(Digest.window_key == window_key)
        .order_by(Digest.version.desc())
    )


def build_digest(
    session: Session,
    *,
    window: DigestWindow | None = None,
    window_key: str | None = None,
    regenerate: bool = False,
) -> tuple[Digest, bool]:
    if window is None and window_key is None:
        raise ValueError("window or window_key is required")
    key = window.key if window is not None else window_key
    assert key is not None
    for _attempt in range(20):
        latest = session.scalar(
            select(Digest).where(Digest.window_key == key).order_by(Digest.version.desc())
        )
        if latest is not None and not regenerate:
            return latest, False
        digest = Digest(
            window_key=key,
            version=1 if latest is None else latest.version + 1,
            timezone=window.timezone if window is not None else _timezone_from_legacy_key(key),
            window_start_at=window.start if window is not None else None,
            window_end_at=window.end if window is not None else None,
        )
        ensure_sqlite_outer_transaction(session)
        try:
            with session.begin_nested():
                session.add(digest)
                session.flush()
        except IntegrityError:
            if not regenerate:
                winner = session.scalar(
                    select(Digest)
                    .where(Digest.window_key == key)
                    .order_by(Digest.version.desc())
                )
                if winner is not None:
                    return winner, False
            continue
        break
    else:
        raise RuntimeError("digest_version_conflict")

    summaries = list(session.scalars(select(Summary).where(Summary.status == "succeeded")).all())
    if window is not None:
        summaries = [
            summary for summary in summaries if _summary_in_window(session, summary, window)
        ]
    entries: list[tuple[tuple[float | int | str, ...], str, str, str, dict[str, object]]] = []
    covered_post_ids: set[str] = set()
    for summary in summaries:
        source_posts = list(
            session.scalars(select(Post).where(Post.id.in_(summary.source_ids))).all()
        )
        covered_post_ids.update(post.id for post in source_posts)
        topic = _topic_slug(summary.topics[0] if summary.topics else None)
        latest_at = max(
            (_utc(post.created_at) for post in source_posts),
            default=datetime.min.replace(tzinfo=UTC),
        )
        snapshot: dict[str, object] = {
            "summary": summary.summary,
            "key_points": list(summary.key_points),
            "source_ids": list(summary.source_ids),
            "sources": [
                {"id": post.id, "url": post.source_url}
                for post in sorted(source_posts, key=lambda post: post.id)
            ],
            "importance": summary.importance,
            "topics": list(summary.topics),
            "language": summary.language,
        }
        entries.append(
            (
                (-summary.importance, -latest_at.timestamp(), summary.id),
                "summary",
                summary.id,
                topic,
                snapshot,
            )
        )

    if window is not None:
        posts = session.scalars(
            select(Post).where(Post.created_at >= window.start, Post.created_at < window.end)
        ).all()
        for post in posts:
            if post.id in covered_post_ids:
                continue
            snapshot = {
                "text": post.text,
                "source_id": post.id,
                "source_url": post.source_url,
                "created_at": _utc(post.created_at).isoformat().replace("+00:00", "Z"),
            }
            entries.append(
                (
                    (-1, -_utc(post.created_at).timestamp(), post.id),
                    "post",
                    post.id,
                    "general",
                    snapshot,
                )
            )

    entries.sort(key=lambda entry: entry[0])
    groups: dict[str, list[dict[str, object]]] = {}
    key_points: list[str] = []
    for position, (_sort_key, source_type, source_id, topic, snapshot) in enumerate(entries):
        session.add(
            DigestItem(
                digest_id=digest.id,
                source_type=source_type,
                source_id=source_id,
                topic=topic,
                position=position,
                snapshot_json=snapshot,
            )
        )
        groups.setdefault(topic, []).append(snapshot)
        points = snapshot.get("key_points")
        if isinstance(points, list):
            for point in points:
                if isinstance(point, str) and point not in key_points:
                    key_points.append(point)
    digest.rendered_content = {
        "title": _title(window, key),
        "summary": (
            f"{len(entries)} items across {len(groups)} topics"
            if entries
            else "No items in this window"
        ),
        "key_points": key_points,
        "groups": [{"topic": topic, "items": items} for topic, items in groups.items()],
    }
    session.flush()
    return digest, True


def _summary_in_window(session: Session, summary: Summary, window: DigestWindow) -> bool:
    posts = session.scalars(select(Post).where(Post.id.in_(summary.source_ids))).all()
    return any(window.start <= _utc(post.created_at) < window.end for post in posts)


def _summary_latest_at(session: Session, summary: Summary) -> datetime:
    posts = session.scalars(select(Post).where(Post.id.in_(summary.source_ids))).all()
    if not posts:
        return datetime.min.replace(tzinfo=UTC)
    return max(_utc(post.created_at) for post in posts)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _timezone_from_legacy_key(key: str | None) -> str:
    if key:
        parts = key.split("/")
        if len(parts) >= 3 and parts[-2]:
            return parts[-2]
    return "UTC"


def _topic_slug(value: str | None) -> str:
    slug = re.sub(r"[^\w]+", "-", (value or "").strip().lower()).strip("-_")
    return slug or "general"


def _title(window: DigestWindow | None, key: str | None) -> str:
    if window is None:
        return f"Digest · {key}"
    start = window.start.isoformat().replace("+00:00", "Z")
    return f"{window.cadence} digest · {start}"
