from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from x_digest.api.jobs import JobService
from x_digest.config import Settings
from x_digest.main import create_app
from x_digest.models import (
    Author,
    Base,
    Digest,
    DigestItem,
    Post,
    PostListMembership,
    PostState,
    Summary,
    XList,
)
from x_digest.services.pipeline import PipelineResult, StageResult
from x_digest.services.summarization import DeterministicSummarizer

TOKEN = "test-only-internal-api-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakePipeline:
    async def run_list_pipeline(self, list_id: str) -> PipelineResult:
        return PipelineResult(
            run_id=f"run-for-{list_id}",
            status="succeeded",
            stages={"ingestion": StageResult(counts={"posts_created": 0}, duration_ms=1)},
        )


class FailingPipeline:
    async def run_list_pipeline(self, list_id: str) -> PipelineResult:
        raise RuntimeError(f"provider secret for {list_id}")


class LeakyPipeline:
    async def run_list_pipeline(self, list_id: str) -> PipelineResult:
        return PipelineResult(
            run_id=f"run-for-{list_id}",
            status="partial",
            stages={
                "links": StageResult(
                    counts={"failed": 1},
                    duration_ms=1,
                    last_error="provider-token-super-secret",
                ),
                "summary": StageResult(
                    counts={"failed": 1},
                    duration_ms=2,
                    last_error="summary_processing_failed",
                ),
            },
        )


@pytest.fixture
def api() -> tuple[TestClient, sessionmaker[Session], dict[str, str]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 9, 11, 12, tzinfo=UTC)
    with factory.begin() as session:
        ai_list = XList(platform_list_id="list-ai", name="AI news", sync_interval_minutes=60)
        other_list = XList(platform_list_id="list-other", name="Other")
        alice = Author(platform_author_id="author-a", username="alice", display_name="Alice")
        bob = Author(platform_author_id="author-b", username="bob", display_name="Bob")
        session.add_all([ai_list, other_list, alice, bob])
        session.flush()
        older = Post(
            platform="x",
            platform_post_id="101",
            author=alice,
            text="Older AI post",
            created_at=now - timedelta(hours=2),
            source_url="https://x.com/alice/status/101",
        )
        tied_a = Post(
            platform="x",
            platform_post_id="102",
            author=alice,
            text="Saved AI post",
            created_at=now - timedelta(hours=1),
            source_url="https://x.com/alice/status/102",
        )
        tied_b = Post(
            platform="x",
            platform_post_id="103",
            author=bob,
            text="Python post",
            created_at=now - timedelta(hours=1),
            source_url="https://x.com/bob/status/103",
        )
        session.add_all([older, tied_a, tied_b])
        session.flush()
        session.add_all(
            [
                PostListMembership(post_id=older.id, list_id=ai_list.id),
                PostListMembership(post_id=tied_a.id, list_id=ai_list.id),
                PostListMembership(post_id=tied_b.id, list_id=ai_list.id),
                PostState(post_id=tied_a.id, is_saved=True),
                Summary(
                    content_fingerprint="ai-a",
                    model="fake-summary-v1",
                    prompt_version="single-content-v1",
                    generation=1,
                    summary="AI summary",
                    key_points=["AI"],
                    topics=["ai"],
                    importance=4,
                    language="en",
                    source_ids=[tied_a.id],
                ),
                Summary(
                    content_fingerprint="python-a",
                    model="fake-summary-v1",
                    prompt_version="single-content-v1",
                    generation=1,
                    summary="Python summary",
                    key_points=["Python"],
                    topics=["python"],
                    importance=3,
                    language="en",
                    source_ids=[tied_b.id],
                ),
            ]
        )
        first_digest = Digest(window_key="2026-09-11/UTC/24h", version=1)
        second_digest = Digest(window_key="2026-09-11/UTC/24h", version=2)
        session.add_all([first_digest, second_digest])
        session.flush()
        session.add(
            DigestItem(
                digest_id=second_digest.id,
                source_type="summary",
                source_id="summary-snapshot",
                topic="ai",
                position=0,
                snapshot_json={"summary": "Version two"},
            )
        )
        ids = {
            "list": ai_list.id,
            "other_list": other_list.id,
            "older": older.id,
            "saved": tied_a.id,
            "python": tied_b.id,
            "digest_v1": first_digest.id,
            "digest_v2": second_digest.id,
        }

    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        internal_api_token=SecretStr(TOKEN),
    )
    app = create_app(
        settings=settings,
        session_factory=factory,
        pipeline_service=FakePipeline(),
    )
    return TestClient(app), factory, ids


def test_list_crud_validates_input_and_missing_resources(api) -> None:
    client, factory, ids = api

    created = client.post(
        "/api/lists",
        headers=AUTH,
        json={
            "platform_list_id": "list-new",
            "name": "Research",
            "sync_interval_minutes": 30,
            "enabled": True,
        },
    )
    assert created.status_code == 201
    assert created.json()["name"] == "Research"
    list_id = created.json()["id"]

    patched = client.patch(
        f"/api/lists/{list_id}", headers=AUTH, json={"name": "Labs", "enabled": False}
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Labs"
    assert patched.json()["enabled"] is False
    assert any(row["id"] == list_id for row in client.get("/api/lists", headers=AUTH).json())

    invalid = client.post(
        "/api/lists",
        headers=AUTH,
        json={"platform_list_id": "", "name": "", "sync_interval_minutes": 0},
    )
    assert invalid.status_code == 422
    assert client.patch(f"/api/lists/{list_id}", headers=AUTH, json={}).status_code == 422
    assert (
        client.patch(f"/api/lists/{list_id}", headers=AUTH, json={"name": None}).status_code == 422
    )

    missing = client.patch("/api/lists/missing", headers=AUTH, json={"name": "Nope"})
    assert missing.status_code == 404
    assert missing.json()["code"] == "list_not_found"
    assert "missing" not in missing.json()["message"]

    assert client.delete(f"/api/lists/{list_id}", headers=AUTH).status_code == 204
    assert client.delete(f"/api/lists/{list_id}", headers=AUTH).status_code == 404
    assert client.delete(f"/api/lists/{ids['list']}", headers=AUTH).status_code == 204
    with factory() as session:
        assert session.get(XList, ids["list"]) is None
        memberships = session.scalars(
            select(PostListMembership).where(PostListMembership.list_id == ids["list"])
        ).all()
        assert memberships == []


def test_request_models_reject_type_coercion(api) -> None:
    client, _factory, ids = api

    numeric_string = client.post(
        "/api/lists",
        headers=AUTH,
        json={
            "platform_list_id": "strict-list-a",
            "name": "Strict",
            "sync_interval_minutes": "30",
        },
    )
    integer_boolean = client.post(
        "/api/lists",
        headers=AUTH,
        json={
            "platform_list_id": "strict-list-b",
            "name": "Strict",
            "enabled": 1,
        },
    )
    state_integer = client.post(
        f"/api/posts/{ids['older']}/state",
        headers=AUTH,
        json={"read": 1},
    )

    assert numeric_string.status_code == 422
    assert integer_boolean.status_code == 422
    assert state_integer.status_code == 422


def test_posts_use_stable_cursor_pagination_and_include_source_url(api) -> None:
    client, factory, ids = api

    first = client.get(f"/api/posts?list_id={ids['list']}&limit=1", headers=AUTH)
    assert first.status_code == 200
    assert len(first.json()["items"]) == 1
    assert first.json()["items"][0]["source_url"].startswith("https://x.com/")
    cursor = first.json()["next_cursor"]
    first_id = first.json()["items"][0]["id"]

    with factory.begin() as session:
        alice = session.scalar(select(Author).where(Author.username == "alice"))
        newest = Post(
            platform="x",
            platform_post_id="999",
            author=alice,
            text="Arrived after page one",
            created_at=datetime(2026, 9, 11, 13, tzinfo=UTC),
            source_url="https://x.com/alice/status/999",
        )
        session.add(newest)
        session.flush()
        session.add(PostListMembership(post_id=newest.id, list_id=ids["list"]))

    second = client.get(f"/api/posts?list_id={ids['list']}&limit=2&cursor={cursor}", headers=AUTH)
    second_ids = [item["id"] for item in second.json()["items"]]
    assert second.status_code == 200
    assert len(second_ids) == 2
    assert first_id not in second_ids
    assert newest.id not in second_ids

    invalid = client.get("/api/posts?cursor=not-a-cursor", headers=AUTH)
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_cursor"


def test_posts_apply_combined_list_topic_author_time_and_state_filters(api) -> None:
    client, _factory, ids = api

    response = client.get(
        "/api/posts",
        headers=AUTH,
        params={
            "list_id": ids["list"],
            "topic": "ai",
            "author": "alice",
            "from": "2026-09-11T10:30:00Z",
            "to": "2026-09-11T12:00:00Z",
            "state": "saved",
            "limit": 10,
        },
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [ids["saved"]]
    assert response.json()["items"][0]["author"]["username"] == "alice"
    assert response.json()["items"][0]["state"] == {
        "read": False,
        "saved": True,
        "ignored": False,
    }


def test_posts_reject_reversed_time_window(api) -> None:
    client, _factory, _ids = api

    response = client.get(
        "/api/posts",
        headers=AUTH,
        params={"from": "2026-09-11T12:00:00Z", "to": "2026-09-11T10:00:00Z"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_time_window"
    assert response.json()["message"] == "Start time must not be after end time"


def test_post_state_mutation_is_idempotent_and_rejects_missing_post(api) -> None:
    client, factory, ids = api

    first = client.post(
        f"/api/posts/{ids['older']}/state",
        headers=AUTH,
        json={"read": True, "saved": True},
    )
    repeated = client.post(
        f"/api/posts/{ids['older']}/state",
        headers=AUTH,
        json={"read": True, "saved": True},
    )

    assert first.status_code == repeated.status_code == 200
    assert first.json() == repeated.json()
    assert first.json() == {"read": True, "saved": True, "ignored": False}
    with factory() as session:
        count = session.scalar(
            select(func.count()).select_from(PostState).where(PostState.post_id == ids["older"])
        )
        assert count == 1
    missing = client.post("/api/posts/missing/state", headers=AUTH, json={"read": True})
    assert missing.status_code == 404
    assert missing.json()["code"] == "post_not_found"
    assert client.post(f"/api/posts/{ids['older']}/state", headers=AUTH, json={}).status_code == 422
    assert (
        client.post(
            f"/api/posts/{ids['older']}/state", headers=AUTH, json={"read": None}
        ).status_code
        == 422
    )


def test_digest_version_retrieval_and_regeneration(api) -> None:
    client, _factory, ids = api

    listing = client.get("/api/digests", headers=AUTH)
    detail = client.get(f"/api/digests/{ids['digest_v2']}", headers=AUTH)
    regenerated = client.post(f"/api/digests/{ids['digest_v1']}/regenerate", headers=AUTH)

    assert [row["version"] for row in listing.json()] == [2, 1]
    assert detail.status_code == 200
    assert detail.json()["version"] == 2
    assert detail.json()["items"][0]["snapshot"]["summary"] == "Version two"
    assert regenerated.status_code == 201
    assert regenerated.json()["version"] == 3
    missing = client.get("/api/digests/missing", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json()["code"] == "digest_not_found"


def test_summary_regeneration_requires_key_and_deduplicates(api) -> None:
    client, factory, ids = api
    path = f"/api/posts/{ids['saved']}/summaries/regenerate"

    assert client.post(path, headers=AUTH).status_code == 422
    first = client.post(path, headers={**AUTH, "Idempotency-Key": "summary-request-1"})
    repeated = client.post(path, headers={**AUTH, "Idempotency-Key": "summary-request-1"})

    assert first.status_code == repeated.status_code == 201
    assert first.json() == repeated.json()
    assert first.json()["generation"] == 2
    mismatched = client.post(
        f"/api/posts/{ids['python']}/summaries/regenerate",
        headers={**AUTH, "Idempotency-Key": "summary-request-1"},
    )
    assert mismatched.status_code == 409
    assert mismatched.json()["code"] == "idempotency_key_reused"
    with factory() as session:
        rows = session.scalars(
            select(Summary).where(Summary.regeneration_request_id == "summary-request-1")
        ).all()
        assert len(rows) == 1


def test_summary_regeneration_rebuilds_the_complete_multi_post_unit(api) -> None:
    client, factory, ids = api
    with factory.begin() as session:
        session.add(
            Summary(
                content_fingerprint="multi-post-unit",
                model="fake-summary-v1",
                prompt_version="single-content-v1",
                generation=5,
                summary="Original thread summary",
                key_points=["thread"],
                topics=["ai"],
                importance=4,
                language="en",
                source_ids=[ids["older"], ids["saved"]],
            )
        )

    first = client.post(
        f"/api/posts/{ids['older']}/summaries/regenerate",
        headers={**AUTH, "Idempotency-Key": "multi-post-request-1"},
    )
    second = client.post(
        f"/api/posts/{ids['saved']}/summaries/regenerate",
        headers={**AUTH, "Idempotency-Key": "multi-post-request-2"},
    )

    assert first.status_code == 201
    assert first.json()["source_ids"] == [ids["older"], ids["saved"]]
    assert "Older AI post" in first.json()["summary"]
    assert "Saved AI post" in first.json()["summary"]
    assert first.json()["generation"] == 6
    assert second.status_code == 201
    assert second.json()["source_ids"] == [ids["older"], ids["saved"]]
    assert second.json()["generation"] == 7


def test_summary_regeneration_uses_latest_unit_not_highest_unrelated_generation(api) -> None:
    client, factory, ids = api
    with factory.begin() as session:
        old_single = next(
            summary
            for summary in session.scalars(select(Summary)).all()
            if summary.source_ids == [ids["saved"]]
        )
        old_single.generation = 9
        old_single.created_at = datetime(2020, 1, 1, tzinfo=UTC)
        session.add(
            Summary(
                content_fingerprint="latest-multi-post-unit",
                model="fake-summary-v1",
                prompt_version="single-content-v1",
                generation=1,
                summary="Latest complete thread summary",
                key_points=["thread"],
                topics=["ai"],
                importance=4,
                language="en",
                source_ids=[ids["older"], ids["saved"]],
                created_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )

    response = client.post(
        f"/api/posts/{ids['saved']}/summaries/regenerate",
        headers={**AUTH, "Idempotency-Key": "latest-unit-request"},
    )

    assert response.status_code == 201
    assert response.json()["source_ids"] == [ids["older"], ids["saved"]]
    assert response.json()["generation"] == 2


def test_summary_regeneration_recovers_same_request_commit_race(api, monkeypatch) -> None:
    _client, factory, ids = api
    session = factory()
    service = JobService(
        session=session,
        pipeline=FakePipeline(),
        summarizer=DeterministicSummarizer(),
    )

    def competing_commit() -> None:
        pending = next(row for row in session.new if isinstance(row, Summary))
        with factory.begin() as competing:
            competing.add(
                Summary(
                    content_fingerprint=pending.content_fingerprint,
                    model=pending.model,
                    prompt_version=pending.prompt_version,
                    generation=pending.generation,
                    regeneration_request_id=pending.regeneration_request_id,
                    summary=pending.summary,
                    key_points=pending.key_points,
                    topics=pending.topics,
                    importance=pending.importance,
                    language=pending.language,
                    source_ids=pending.source_ids,
                    token_usage=pending.token_usage,
                    status=pending.status,
                )
            )
        raise IntegrityError("insert summary", {}, RuntimeError("unique conflict"))

    monkeypatch.setattr(session, "commit", competing_commit)
    try:
        recovered = asyncio.run(
            service.regenerate_summary(ids["older"], "concurrent-summary-request")
        )
    finally:
        session.close()

    assert recovered.regeneration_request_id == "concurrent-summary-request"
    with factory() as check:
        rows = check.scalars(
            select(Summary).where(
                Summary.regeneration_request_id == "concurrent-summary-request"
            )
        ).all()
        assert len(rows) == 1
        assert recovered.id == rows[0].id


def test_sync_job_returns_pipeline_result_and_status_is_safe(api) -> None:
    client, _factory, ids = api

    result = client.post(f"/api/jobs/sync/{ids['list']}", headers=AUTH)
    status = client.get("/api/status", headers=AUTH)

    assert result.status_code == 200
    assert result.json()["run_id"] == f"run-for-{ids['list']}"
    assert result.json()["stages"]["ingestion"]["counts"] == {"posts_created": 0}
    assert status.status_code == 200
    assert set(status.json()) == {"latest_run", "latest_summary", "deliveries"}
    missing = client.post("/api/jobs/sync/missing", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json()["code"] == "list_not_found"


def test_unexpected_api_errors_never_expose_exception_text(api) -> None:
    client, _factory, ids = api
    client.app.state.pipeline_service = FailingPipeline()
    safe_client = TestClient(client.app, raise_server_exceptions=False)

    response = safe_client.post(f"/api/jobs/sync/{ids['list']}", headers=AUTH)

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert response.json()["message"] == "Internal server error"
    assert response.json()["request_id"]
    assert "provider secret" not in str(response.json())


def test_unexpected_api_errors_are_logged_with_request_id(api, caplog) -> None:
    client, _factory, ids = api
    client.app.state.pipeline_service = FailingPipeline()
    safe_client = TestClient(client.app, raise_server_exceptions=False)

    with caplog.at_level(logging.ERROR, logger="x_digest.api.errors"):
        response = safe_client.post(f"/api/jobs/sync/{ids['list']}", headers=AUTH)

    record = next(record for record in caplog.records if record.message == "Unexpected API error")
    assert record.request_id == response.json()["request_id"]


def test_sync_job_omits_unknown_pipeline_error_text(api) -> None:
    client, _factory, ids = api
    client.app.state.pipeline_service = LeakyPipeline()

    response = client.post(f"/api/jobs/sync/{ids['list']}", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["stages"]["links"]["last_error"] is None
    assert (
        response.json()["stages"]["summary"]["last_error"]
        == "summary_processing_failed"
    )
    assert "super-secret" not in str(response.json())


def test_openapi_exposes_all_protected_api_paths(api) -> None:
    client, _factory, _ids = api

    paths = client.get("/openapi.json").json()["paths"]

    assert {
        "/api/lists",
        "/api/lists/{list_id}",
        "/api/posts",
        "/api/posts/{post_id}/state",
        "/api/posts/{post_id}/summaries/regenerate",
        "/api/digests",
        "/api/digests/{digest_id}",
        "/api/digests/{digest_id}/regenerate",
        "/api/jobs/sync/{list_id}",
        "/api/status",
    } <= set(paths)
    assert all(
        operation.get("security")
        for path, operations in paths.items()
        if path.startswith("/api/")
        for operation in operations.values()
    )
