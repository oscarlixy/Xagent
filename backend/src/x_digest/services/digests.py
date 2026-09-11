from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from x_digest.models import Digest, DigestItem, Summary


def build_digest(
    session: Session, *, window_key: str, regenerate: bool = False
) -> tuple[Digest, bool]:
    latest = session.scalar(
        select(Digest).where(Digest.window_key == window_key).order_by(desc(Digest.version))
    )
    if latest is not None and not regenerate:
        return latest, False

    version = 1 if latest is None else latest.version + 1
    digest = Digest(window_key=window_key, version=version)
    session.add(digest)
    session.flush()

    summaries = session.scalars(
        select(Summary)
        .where(Summary.status == "succeeded")
        .order_by(desc(Summary.importance), Summary.id)
    ).all()
    for position, summary in enumerate(summaries):
        session.add(
            DigestItem(
                digest_id=digest.id,
                source_type="summary",
                source_id=summary.id,
                topic=summary.topics[0] if summary.topics else None,
                position=position,
                snapshot_json={
                    "summary": summary.summary,
                    "source_ids": summary.source_ids,
                    "importance": summary.importance,
                    "topics": summary.topics,
                },
            )
        )
    session.flush()
    return digest, True
