from datetime import UTC, datetime, timedelta

from x_digest.services.threading import ThreadPostView, assemble_threads


def test_explicit_reply_chain_is_ordered_even_when_input_is_not() -> None:
    created = datetime.now(UTC)
    posts = [
        ThreadPostView("2", "author", created + timedelta(minutes=1), "1", "1"),
        ThreadPostView("1", "author", created, "1", None),
        ThreadPostView("3", "author", created + timedelta(minutes=2), "1", "2"),
    ]

    groups = assemble_threads(posts)

    assert len(groups) == 1
    assert [post.id for post in groups[0].posts] == ["1", "2", "3"]


def test_posts_in_different_conversations_are_not_merged() -> None:
    created = datetime.now(UTC)

    groups = assemble_threads(
        [
            ThreadPostView("1", "author", created, "conversation-a", None),
            ThreadPostView("2", "author", created, "conversation-b", None),
        ]
    )

    assert len(groups) == 2
