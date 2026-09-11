from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from x_digest.models import (
    Author,
    Base,
    Digest,
    NotificationDelivery,
    Post,
    PostListMembership,
    Summary,
    XList,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session


def make_post(session: Session, platform_post_id: str = "42") -> Post:
    author = Author(platform_author_id="author-1", username="author")
    post = Post(
        platform="x",
        platform_post_id=platform_post_id,
        author=author,
        text="A post",
        created_at=datetime.now(UTC),
        source_url=f"https://x.com/author/status/{platform_post_id}",
    )
    session.add(post)
    return post


def test_post_is_stored_once_and_can_belong_to_two_lists(session: Session) -> None:
    post = make_post(session)
    first_list = XList(platform_list_id="list-a", name="First")
    second_list = XList(platform_list_id="list-b", name="Second")
    session.add_all(
        [
            PostListMembership(post=post, x_list=first_list),
            PostListMembership(post=post, x_list=second_list),
        ]
    )
    session.commit()

    assert session.query(Post).count() == 1
    assert session.query(PostListMembership).count() == 2

    make_post(session, platform_post_id="42")
    with pytest.raises(IntegrityError):
        session.commit()


def test_summary_generation_and_digest_versions_are_preserved(session: Session) -> None:
    post = make_post(session)
    session.add(
        Summary(
            content_fingerprint="fingerprint",
            model="fake-model",
            prompt_version="single-content-v1",
            generation=1,
            summary="中文摘要",
            key_points=["重点"],
            topics=["ai"],
            importance=3,
            language="zh",
            source_ids=[post.id],
        )
    )
    first = Digest(window_key="2026-09-11T00:00Z/24h", version=1)
    second = Digest(window_key=first.window_key, version=2)
    session.add_all([first, second])
    session.commit()

    assert first.id != second.id

    session.add(
        Summary(
            content_fingerprint="fingerprint",
            model="fake-model",
            prompt_version="single-content-v1",
            generation=1,
            summary="重复",
            key_points=["重点"],
            topics=["ai"],
            importance=3,
            language="zh",
            source_ids=[post.id],
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_delivery_can_be_claimed_once_per_digest_and_channel(session: Session) -> None:
    digest = Digest(window_key="2026-09-11T00:00Z/24h", version=1)
    session.add(digest)
    session.flush()
    session.add(NotificationDelivery(digest_id=digest.id, channel="telegram", status="pending"))
    session.commit()

    session.add(NotificationDelivery(digest_id=digest.id, channel="telegram", status="pending"))
    with pytest.raises(IntegrityError):
        session.commit()
