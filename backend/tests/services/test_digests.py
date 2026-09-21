import asyncio
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from x_digest.ai.contracts import DigestEventOutput, DigestOutput, DigestTopicOutput
from x_digest.ai.openai import LLMProviderError
from x_digest.models import Author, Base, Digest, DigestItem, Post, Summary
from x_digest.services.digest_synthesis import DigestProviderResult
from x_digest.services.digests import (
    DigestWindow,
    build_digest,
    generate_digest,
    window_for,
)


def test_window_for_returns_latest_completed_local_window() -> None:
    hong_kong = ZoneInfo("Asia/Hong_Kong")

    daily = window_for(datetime(2026, 9, 16, 8, 30, tzinfo=hong_kong), "daily", hong_kong)
    six_hour = window_for(datetime(2026, 9, 16, 10, 30, tzinfo=hong_kong), "6h", hong_kong)

    assert daily.start == datetime(2026, 9, 14, 16, tzinfo=UTC)
    assert daily.end == datetime(2026, 9, 15, 16, tzinfo=UTC)
    assert six_hour.start == datetime(2026, 9, 15, 16, tzinfo=UTC)
    assert six_hour.end == datetime(2026, 9, 15, 22, tzinfo=UTC)


def test_daily_window_tracks_local_midnight_across_dst_changes() -> None:
    new_york = ZoneInfo("America/New_York")

    spring = window_for(datetime(2026, 3, 9, 8, tzinfo=new_york), "daily", new_york)
    fall = window_for(datetime(2026, 11, 2, 8, tzinfo=new_york), "daily", new_york)

    assert spring.start == datetime(2026, 3, 8, 5, tzinfo=UTC)
    assert spring.end == datetime(2026, 3, 9, 4, tzinfo=UTC)
    assert fall.start == datetime(2026, 11, 1, 4, tzinfo=UTC)
    assert fall.end == datetime(2026, 11, 2, 5, tzinfo=UTC)


def test_digest_only_includes_summaries_with_sources_inside_half_open_window() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    window = DigestWindow(
        start=datetime(2026, 9, 16, 0, tzinfo=UTC),
        end=datetime(2026, 9, 16, 6, tzinfo=UTC),
        cadence="6h",
        timezone="UTC",
    )
    with Session(engine) as session:
        author = Author(platform_author_id="author-a", username="alice")
        session.add(author)
        session.flush()
        posts = [
            Post(
                platform="x",
                platform_post_id=str(index),
                author_id=author.id,
                text=f"post {index}",
                created_at=created_at,
                source_url=f"https://x.com/alice/status/{index}",
            )
            for index, created_at in enumerate(
                [
                    datetime(2026, 9, 16, 0, tzinfo=UTC),
                    datetime(2026, 9, 16, 6, tzinfo=UTC),
                ]
            )
        ]
        session.add_all(posts)
        session.flush()
        for label, source_ids in (
            ("inside", [posts[0].id]),
            ("at-end", [posts[1].id]),
            ("missing", ["missing-post"]),
        ):
            session.add(
                Summary(
                    content_fingerprint=label,
                    model="fake-summary-v1",
                    prompt_version="single-content-v1",
                    generation=1,
                    summary=label,
                    key_points=[label],
                    topics=["ai"],
                    importance=3,
                    language="en",
                    source_ids=source_ids,
                )
            )
        session.flush()

        digest, _ = build_digest(session, window=window)
        session.flush()

        items = session.scalars(
            select(DigestItem).where(DigestItem.digest_id == digest.id)
        ).all()
        assert [item.snapshot_json["summary"] for item in items] == ["inside"]


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


def test_digest_renders_topic_groups_and_unsummarized_post_fallback() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    window = DigestWindow(
        start=datetime(2026, 9, 16, 0, tzinfo=UTC),
        end=datetime(2026, 9, 16, 6, tzinfo=UTC),
        cadence="6h",
        timezone="UTC",
    )
    with Session(engine) as session:
        author = Author(platform_author_id="author-a", username="alice")
        session.add(author)
        session.flush()
        covered = Post(
            platform="x",
            platform_post_id="covered",
            author_id=author.id,
            text="Covered post",
            created_at=datetime(2026, 9, 16, 1, tzinfo=UTC),
            source_url="https://x.com/alice/status/covered",
        )
        fallback = Post(
            platform="x",
            platform_post_id="fallback",
            author_id=author.id,
            text="Unsummarized post",
            created_at=datetime(2026, 9, 16, 2, tzinfo=UTC),
            source_url="https://x.com/alice/status/fallback",
        )
        session.add_all([covered, fallback])
        session.flush()
        session.add(
            Summary(
                content_fingerprint="covered",
                model="fake-summary-v1",
                prompt_version="single-content-v1",
                generation=1,
                summary="Research summary",
                key_points=["Finding one"],
                topics=[" AI Research! "],
                importance=5,
                language="en",
                source_ids=[covered.id],
            )
        )
        session.flush()

        digest, created = build_digest(session, window=window)
        session.flush()
        items = session.scalars(
            select(DigestItem)
            .where(DigestItem.digest_id == digest.id)
            .order_by(DigestItem.position)
        ).all()

        assert created is True
        assert [(item.source_type, item.topic) for item in items] == [
            ("summary", "ai-research"),
            ("post", "general"),
        ]
        assert items[0].snapshot_json["sources"] == [
            {"id": covered.id, "url": covered.source_url}
        ]
        assert items[1].snapshot_json["source_url"] == fallback.source_url
        assert digest.rendered_content == {
            "title": "6h digest · 2026-09-16T00:00:00Z",
            "summary": "2 items across 2 topics",
            "key_points": ["Finding one"],
            "groups": [
                {
                    "topic": "ai-research",
                    "items": [items[0].snapshot_json],
                },
                {"topic": "general", "items": [items[1].snapshot_json]},
            ],
        }


def test_empty_digest_has_complete_rendered_content() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    window = DigestWindow(
        start=datetime(2026, 9, 16, 0, tzinfo=UTC),
        end=datetime(2026, 9, 16, 6, tzinfo=UTC),
        cadence="6h",
        timezone="UTC",
    )

    with Session(engine) as session:
        digest, _ = build_digest(session, window=window)

        assert digest.rendered_content == {
            "title": "6h digest · 2026-09-16T00:00:00Z",
            "summary": "No items in this window",
            "key_points": [],
            "groups": [],
        }


class FakeDigestSynthesizer:
    model_name = "fake-digest-v1"

    def __init__(self, output: DigestOutput) -> None:
        self.output = output
        self.calls = 0

    async def synthesize_digest(self, _candidates):
        self.calls += 1
        return DigestProviderResult(output=self.output, token_usage={"input_tokens": 10})


class RaisingDigestSynthesizer:
    model_name = "raising-digest-v1"

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def synthesize_digest(self, _candidates):
        self.calls += 1
        raise self.error


def _event_fixture(session: Session) -> tuple[DigestWindow, list[Post]]:
    window = DigestWindow(
        start=datetime(2026, 9, 16, 0, tzinfo=UTC),
        end=datetime(2026, 9, 16, 6, tzinfo=UTC),
        cadence="6h",
        timezone="UTC",
    )
    author = Author(platform_author_id="event-author", username="alice")
    session.add(author)
    session.flush()
    posts = [
        Post(
            platform="x",
            platform_post_id=f"event-{index}",
            author_id=author.id,
            text=text,
            created_at=window.start.replace(hour=index + 1),
            source_url=f"https://x.com/alice/status/event-{index}",
        )
        for index, text in enumerate(("Agent launch", "Tool update", "Promotional post"))
    ]
    session.add_all(posts)
    session.flush()
    for index, post in enumerate(posts):
        session.add(
            Summary(
                content_fingerprint=f"event-summary-{index}",
                model="fake-summary-v1",
                prompt_version="single-content-v1",
                generation=1,
                summary=post.text,
                key_points=[f"point-{index}"],
                topics=["Agent" if index == 0 else "工具"],
                importance=5 - index,
                language="en",
                source_ids=[post.id],
            )
        )
    session.flush()
    return window, posts


def _event_output(posts: list[Post]) -> DigestOutput:
    return DigestOutput(
        overview="本时段主要关注 Agent 与工具更新。",
        topics=[
            DigestTopicOutput(
                name="Agent",
                events=[
                    DigestEventOutput(
                        title="Agent 发布",
                        what_happened="Alice 发布了 Agent。",
                        why_it_matters="展示了新的自动化能力。",
                        event_type="product",
                        importance=5,
                        source_ids=[posts[0].id],
                    )
                ],
            ),
            DigestTopicOutput(
                name="工具",
                events=[
                    DigestEventOutput(
                        title="工具更新",
                        what_happened="工具增加了新功能。",
                        why_it_matters="降低了使用成本。",
                        event_type="news",
                        importance=4,
                        source_ids=[posts[1].id],
                    )
                ],
            ),
        ],
        excluded_source_ids=[posts[2].id],
    )


def test_generate_digest_persists_full_event_structure_and_immutable_snapshots() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        window, posts = _event_fixture(session)
        synthesizer = FakeDigestSynthesizer(_event_output(posts))

        digest, created = asyncio.run(
            generate_digest(session, synthesizer=synthesizer, window=window)
        )
        items = session.scalars(
            select(DigestItem)
            .where(DigestItem.digest_id == digest.id)
            .order_by(DigestItem.position)
        ).all()

        assert created is True
        assert [item.source_type for item in items] == ["event", "event"]
        assert digest.rendered_content == {
            "schema_version": "event-digest-v1",
            "title": "6h digest · 2026-09-16T00:00:00Z",
            "overview": "本时段主要关注 Agent 与工具更新。",
            "topics": [
                {
                    "name": "Agent",
                    "events": [{**items[0].snapshot_json, "item_id": items[0].id}],
                },
                {
                    "name": "工具",
                    "events": [{**items[1].snapshot_json, "item_id": items[1].id}],
                },
            ],
            "excluded_source_ids": [posts[2].id],
        }
        assert items[0].snapshot_json == {
            "title": "Agent 发布",
            "what_happened": "Alice 发布了 Agent。",
            "why_it_matters": "展示了新的自动化能力。",
            "event_type": "product",
            "importance": 5,
            "source_ids": [posts[0].id],
            "sources": [
                {
                    "id": posts[0].id,
                    "username": "alice",
                    "text": "Agent launch",
                    "url": posts[0].source_url,
                }
            ],
        }


def test_generate_digest_falls_back_on_provider_or_validation_failure() -> None:
    for failure in (LLMProviderError("safe failure"), None):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            window, posts = _event_fixture(session)
            if failure is not None:
                synthesizer = RaisingDigestSynthesizer(failure)
            else:
                invalid = _event_output(posts).model_copy(
                    update={"excluded_source_ids": ["unknown-source"]}
                )
                synthesizer = FakeDigestSynthesizer(invalid)

            digest, created = asyncio.run(
                generate_digest(session, synthesizer=synthesizer, window=window)
            )
            items = session.scalars(
                select(DigestItem).where(DigestItem.digest_id == digest.id)
            ).all()

            assert created is True
            assert len(items) == 3
            assert all(item.source_type == "event" for item in items)
            assert digest.rendered_content["schema_version"] == "event-digest-v1"


def test_generate_digest_is_idempotent_and_regeneration_keeps_version_one() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        window, posts = _event_fixture(session)
        synthesizer = FakeDigestSynthesizer(_event_output(posts))

        first, first_created = asyncio.run(
            generate_digest(session, synthesizer=synthesizer, window=window)
        )
        first_content = first.rendered_content
        repeated, repeated_created = asyncio.run(
            generate_digest(session, synthesizer=synthesizer, window=window)
        )
        regenerated, regenerated_created = asyncio.run(
            generate_digest(
                session, synthesizer=synthesizer, window=window, regenerate=True
            )
        )

        assert first_created is True
        assert repeated_created is False
        assert repeated.id == first.id
        assert synthesizer.calls == 2
        assert regenerated_created is True
        assert regenerated.version == 2
        assert first.version == 1
        assert first.rendered_content == first_content


def test_generate_digest_with_all_candidates_excluded_has_no_items() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        window, posts = _event_fixture(session)
        output = DigestOutput(
            overview="本时段没有值得保留的动态。",
            topics=[],
            excluded_source_ids=[post.id for post in posts],
        )

        digest, _ = asyncio.run(
            generate_digest(
                session, synthesizer=FakeDigestSynthesizer(output), window=window
            )
        )

        assert digest.rendered_content["overview"] == "本时段没有值得保留的动态。"
        assert digest.rendered_content["topics"] == []
        assert session.scalars(
            select(DigestItem).where(DigestItem.digest_id == digest.id)
        ).all() == []


def test_generate_digest_never_uses_stale_cross_author_summary() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        window, posts = _event_fixture(session)
        session.add(
            Summary(
                content_fingerprint="stale-cross-author",
                model="fake-summary-v1",
                prompt_version="single-content-v1",
                generation=2,
                summary="Stale cross-author merge",
                key_points=["must not appear"],
                topics=["wrong"],
                importance=5,
                language="en",
                source_ids=[post.id for post in posts],
                created_at=window.end,
            )
        )
        session.flush()
        synthesizer = RaisingDigestSynthesizer(LLMProviderError("safe failure"))

        digest, _ = asyncio.run(
            generate_digest(session, synthesizer=synthesizer, window=window)
        )

        assert "Stale cross-author merge" not in json.dumps(digest.rendered_content)
