from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from x_digest.models import Author, Base, Post, Summary, Thread, ThreadPost
from x_digest.services.digest_candidates import load_digest_candidates
from x_digest.services.digest_windows import DigestWindow


def window() -> DigestWindow:
    return DigestWindow(
        start=datetime(2026, 9, 20, 0, tzinfo=UTC),
        end=datetime(2026, 9, 21, 0, tzinfo=UTC),
        cadence="daily",
        timezone="UTC",
    )


def post(
    author: Author,
    platform_id: str,
    created_at: datetime,
    *,
    text: str,
    conversation_id: str,
) -> Post:
    return Post(
        platform="x",
        platform_post_id=platform_id,
        author_id=author.id,
        text=text,
        created_at=created_at,
        source_url=f"https://x.com/{author.username}/status/{platform_id}",
        conversation_id=conversation_id,
    )


def summary(
    fingerprint: str,
    source_ids: list[str],
    *,
    text: str,
    created_at: datetime,
) -> Summary:
    return Summary(
        content_fingerprint=fingerprint,
        model="model-a",
        prompt_version="single-content-v1",
        generation=1,
        summary=text,
        key_points=[f"{text} key point"],
        topics=["agents"],
        importance=4,
        language="en",
        source_ids=source_ids,
        created_at=created_at,
    )


def test_selects_exact_current_units_and_ignores_newer_stale_merge() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    digest_window = window()

    with Session(engine) as session:
        alice = Author(platform_author_id="alice-id", username="alice")
        bob = Author(platform_author_id="bob-id", username="bob")
        session.add_all([alice, bob])
        session.flush()
        standalone = post(
            bob,
            "b1",
            digest_window.start + timedelta(hours=1),
            text="Standalone product update",
            conversation_id="conversation-b",
        )
        thread_root = post(
            alice,
            "a1",
            digest_window.start + timedelta(hours=2),
            text="Thread root",
            conversation_id="conversation-a",
        )
        thread_reply = post(
            alice,
            "a2",
            digest_window.start + timedelta(hours=3),
            text="Thread reply",
            conversation_id="conversation-a",
        )
        session.add_all([standalone, thread_root, thread_reply])
        session.flush()
        thread = Thread(
            conversation_id="conversation-a",
            author_id=alice.id,
            created_at=thread_root.created_at,
        )
        session.add(thread)
        session.flush()
        session.add_all(
            [
                ThreadPost(thread_id=thread.id, post_id=thread_root.id, position=0),
                ThreadPost(thread_id=thread.id, post_id=thread_reply.id, position=1),
            ]
        )
        exact_summary = summary(
            "exact",
            [thread_root.id, thread_reply.id],
            text="Exact thread summary",
            created_at=digest_window.start + timedelta(hours=4),
        )
        stale_cross_author_summary = summary(
            "stale",
            [thread_root.id, thread_reply.id, standalone.id],
            text="Stale cross-author merge",
            created_at=digest_window.start + timedelta(hours=5),
        )
        session.add_all([exact_summary, stale_cross_author_summary])
        session.flush()

        candidates = load_digest_candidates(session, digest_window)

        assert [candidate.source_ids for candidate in candidates] == [
            (thread_root.id, thread_reply.id),
            (standalone.id,),
        ]
        assert candidates[0].summary_id == exact_summary.id
        assert candidates[0].summary_id != stale_cross_author_summary.id
        assert candidates[0].summary == "Exact thread summary"
        assert [source.username for source in candidates[0].sources] == ["alice", "alice"]
        assert candidates[1].summary_id is None
        assert candidates[1].summary == standalone.text


def test_thread_with_reply_in_window_includes_root_before_window() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    digest_window = window()

    with Session(engine) as session:
        author = Author(platform_author_id="author-id", username="author")
        session.add(author)
        session.flush()
        root = post(
            author,
            "root",
            digest_window.start - timedelta(minutes=5),
            text="Root before window",
            conversation_id="conversation",
        )
        reply = post(
            author,
            "reply",
            digest_window.start + timedelta(minutes=5),
            text="Reply inside window",
            conversation_id="conversation",
        )
        session.add_all([root, reply])
        session.flush()
        thread = Thread(
            conversation_id="conversation", author_id=author.id, created_at=root.created_at
        )
        session.add(thread)
        session.flush()
        session.add_all(
            [
                ThreadPost(thread_id=thread.id, post_id=root.id, position=0),
                ThreadPost(thread_id=thread.id, post_id=reply.id, position=1),
            ]
        )
        exact_summary = summary(
            "complete-thread",
            [root.id, reply.id],
            text="Complete thread summary",
            created_at=digest_window.start + timedelta(minutes=10),
        )
        session.add(exact_summary)
        session.flush()

        candidates = load_digest_candidates(session, digest_window)

        assert len(candidates) == 1
        assert candidates[0].source_ids == (root.id, reply.id)
        assert candidates[0].summary_id == exact_summary.id
