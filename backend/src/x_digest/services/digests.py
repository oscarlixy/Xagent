import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from x_digest.models import Digest, DigestItem, Post, Summary
from x_digest.repositories.transactions import ensure_sqlite_outer_transaction
from x_digest.services.digest_windows import DigestWindow as DigestWindow
from x_digest.services.digest_windows import window_for as window_for


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
