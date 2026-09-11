from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from x_digest.models import Base, Digest, DigestItem, Summary
from x_digest.services.digests import build_digest


def test_digest_is_idempotent_until_explicit_regeneration() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Summary(
                content_fingerprint="summary-a",
                model="fake-summary-v1",
                prompt_version="single-content-v1",
                generation=1,
                summary="中文摘要",
                key_points=["重点"],
                topics=["ai"],
                importance=4,
                language="zh",
                source_ids=["101"],
            )
        )
        session.flush()

        first, created = build_digest(session, window_key="2026-09-11T00:00Z/24h")
        repeated, created_again = build_digest(session, window_key="2026-09-11T00:00Z/24h")
        regenerated, regenerated_created = build_digest(
            session, window_key="2026-09-11T00:00Z/24h", regenerate=True
        )
        session.commit()

        assert created is True
        assert created_again is False
        assert repeated.id == first.id
        assert regenerated_created is True
        assert regenerated.version == 2
        assert len(session.scalars(select(Digest)).all()) == 2
        assert len(session.scalars(select(DigestItem)).all()) == 2
