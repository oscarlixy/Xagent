from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from x_digest.models import Author, Base, Post, PostListMembership
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


def test_upsert_recovers_when_a_concurrent_insert_wins() -> None:
    author = Author(id="author-db", platform_author_id="author-1", username="author")
    winner = Post(
        id="post-db",
        platform="x",
        platform_post_id="42",
        author=author,
        text="winner",
        created_at=datetime.now(UTC),
        source_url="https://x.com/author/status/42",
    )
    fake = _RacingSession([None, author, winner], fail_type=Post)

    post, created = upsert_post(fake, _post_input())  # type: ignore[arg-type]

    assert post is winner
    assert created is False


def test_attach_recovers_when_a_concurrent_membership_insert_wins() -> None:
    winner = PostListMembership(id="membership-db", post_id="post-db", list_id="list-db")
    fake = _RacingSession([None, winner], fail_type=PostListMembership)

    assert attach_to_list(fake, "post-db", "list-db") is False  # type: ignore[arg-type]


def _post_input() -> PostInput:
    return PostInput(
        platform_post_id="42",
        author=AuthorInput(platform_author_id="author-1", username="author"),
        text="A post",
        created_at=datetime.now(UTC),
        source_url="https://x.com/author/status/42",
    )


class _Nested:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


class _RacingSession:
    def __init__(self, scalar_results: list[object], *, fail_type: type[object]) -> None:
        self._scalar_results = iter(scalar_results)
        self._fail_type = fail_type
        self._pending: object | None = None
        self._failed = False

    def scalar(self, _statement: object) -> object:
        return next(self._scalar_results)

    def add(self, value: object) -> None:
        self._pending = value

    def flush(self) -> None:
        if isinstance(self._pending, self._fail_type) and not self._failed:
            self._failed = True
            raise IntegrityError("concurrent insert", {}, RuntimeError("unique violation"))

    def begin_nested(self) -> _Nested:
        return _Nested()
