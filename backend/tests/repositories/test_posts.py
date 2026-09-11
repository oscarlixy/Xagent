from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from x_digest.models import Base, Post, PostListMembership
from x_digest.repositories.lists import upsert_list
from x_digest.repositories.posts import AuthorInput, PostInput, attach_to_list, upsert_post


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session


def test_upsert_stores_one_post_with_two_list_memberships(session: Session) -> None:
    first_list = upsert_list(session, platform_list_id="list-a", name="First")
    second_list = upsert_list(session, platform_list_id="list-b", name="Second")
    post_input = PostInput(
        platform_post_id="42",
        author=AuthorInput(platform_author_id="author-1", username="author"),
        text="A post",
        created_at=datetime.now(UTC),
        source_url="https://x.com/author/status/42",
    )

    post, created = upsert_post(session, post_input)
    assert created is True
    assert attach_to_list(session, post.id, first_list.id) is True
    assert attach_to_list(session, post.id, second_list.id) is True
    session.commit()

    duplicate, created = upsert_post(session, post_input)
    assert created is False
    assert duplicate.id == post.id
    assert attach_to_list(session, post.id, first_list.id) is False
    session.commit()

    assert session.query(Post).count() == 1
    assert session.query(PostListMembership).count() == 2
