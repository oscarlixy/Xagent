import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from x_digest import scheduler as scheduler_module
from x_digest.ai.fallback import DeterministicDigestSynthesizer
from x_digest.config import Settings
from x_digest.models import Author, Base, Digest, Post, Summary, XList
from x_digest.repositories.lists import upsert_list
from x_digest.scheduler import (
    DigestRunResult,
    PipelineSchedulerServices,
    _run_digest,
    _run_startup_recovery,
    build_scheduler,
    reconcile_list_jobs,
    run_offline_digest,
    schedule_notification,
)


def make_services(
    *, enabled: bool = True, interval: int = 15
) -> tuple[PipelineSchedulerServices, str]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    with session_factory.begin() as session:
        x_list = upsert_list(
            session,
            platform_list_id="list-a",
            name="AI news",
            enabled=enabled,
            sync_interval_minutes=interval,
        )
        list_id = x_list.id
    return (
        PipelineSchedulerServices(
            session_factory=session_factory, pipeline_runner=lambda _list_id: None
        ),
        list_id,
    )


def test_scheduler_pipeline_receives_bounded_ingestion_settings(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def capture_ingestion(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(scheduler_module, "IngestionService", capture_ingestion)
    session_factory = sessionmaker(create_engine("sqlite+pysqlite:///:memory:"))
    _pipeline, oauth_client, source = scheduler_module.build_production_pipeline(
        Settings(_env_file=None), session_factory
    )
    asyncio.run(source.aclose())
    asyncio.run(oauth_client.aclose())

    assert {
        key: captured[key]
        for key in ("page_size", "max_posts", "max_pages", "overlap", "backfill_window")
    } == {
        "page_size": 10,
        "max_posts": 50,
        "max_pages": 6,
        "overlap": timedelta(minutes=10),
        "backfill_window": timedelta(hours=24),
    }


def test_scheduler_adds_enabled_list_job_with_safe_execution_defaults() -> None:
    services, list_id = make_services()
    before_build = datetime.now(UTC)

    scheduler = build_scheduler(Settings(app_timezone="UTC", digest_cadence="daily"), services)

    job = scheduler.get_job(f"sync-list:{list_id}")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 15 * 60
    assert job.coalesce is True
    assert job.max_instances == 1
    assert job.misfire_grace_time is None

    digest_job = scheduler.get_job("digest:daily")
    assert digest_job is not None
    assert digest_job.misfire_grace_time is None

    recovery_job = scheduler.get_job("startup-recovery")
    assert recovery_job is not None
    assert recovery_job.misfire_grace_time is None
    assert before_build <= recovery_job.trigger.run_date <= datetime.now(UTC)


def test_scheduler_removes_disabled_list_jobs_and_replaces_changed_intervals() -> None:
    services, list_id = make_services(interval=15)
    scheduler = build_scheduler(Settings(app_timezone="UTC"), services)
    with services.session_factory.begin() as session:
        x_list = session.get(XList, list_id)
        assert x_list is not None
        x_list.sync_interval_minutes = 30
    reconcile_list_jobs(scheduler, services)
    assert scheduler.get_job(f"sync-list:{list_id}").trigger.interval.total_seconds() == 30 * 60
    with services.session_factory.begin() as session:
        x_list = session.get(XList, list_id)
        assert x_list is not None
        x_list.enabled = False
    reconcile_list_jobs(scheduler, services)
    assert scheduler.get_job(f"sync-list:{list_id}") is None


def test_scheduler_uses_stable_ids_and_only_configured_timezone_aware_digest_jobs() -> None:
    services, list_id = make_services()

    scheduler = build_scheduler(
        Settings(app_timezone="Asia/Hong_Kong", digest_cadence="both"), services
    )

    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert f"sync-list:{list_id}" in jobs
    assert {job_id for job_id in jobs if job_id.startswith("digest:")} == {
        "digest:6h",
        "digest:daily",
    }
    assert all(
        str(jobs[job_id].trigger.timezone) == "Asia/Hong_Kong"
        for job_id in jobs
        if job_id.startswith("digest:")
    )
    reconcile_list_jobs(scheduler, services)
    assert scheduler.get_job(f"sync-list:{list_id}").id == f"sync-list:{list_id}"


def test_running_reconciliation_preserves_unchanged_list_job_schedule() -> None:
    services, list_id = make_services(interval=15)
    scheduler = BackgroundScheduler(timezone="UTC")
    reconcile_list_jobs(scheduler, services)
    added_at = datetime.now(UTC)
    assert scheduler.get_job(f"sync-list:{list_id}").next_run_time <= added_at
    scheduler.start(paused=True)
    try:
        before = scheduler.get_job(f"sync-list:{list_id}")
        assert before is not None
        next_run_time = before.next_run_time

        reconcile_list_jobs(scheduler, services)

        after = scheduler.get_job(f"sync-list:{list_id}")
        assert after is not None
        assert after.next_run_time == next_run_time
    finally:
        scheduler.shutdown(wait=False)


def test_startup_recovery_syncs_lists_before_building_digests() -> None:
    services, list_id = make_services()
    events: list[str] = []
    recovery_services = PipelineSchedulerServices(
        session_factory=services.session_factory,
        pipeline_runner=lambda current_list_id: events.append(f"sync:{current_list_id}"),
        digest_runner=lambda cadence: events.append(f"digest:{cadence}"),
    )

    _run_startup_recovery(recovery_services, (list_id,), ("daily",))

    assert events == [f"sync:{list_id}", "digest:daily"]


def test_startup_recovery_continues_after_a_list_failure_without_logging_details(caplog) -> None:
    services, list_id = make_services()
    events: list[str] = []

    def run_pipeline(current_list_id: str) -> None:
        events.append(f"sync:{current_list_id}")
        if current_list_id == "failing-list":
            raise RuntimeError("provider-secret-must-not-leak")

    recovery_services = PipelineSchedulerServices(
        session_factory=services.session_factory,
        pipeline_runner=run_pipeline,
        digest_runner=lambda cadence: events.append(f"digest:{cadence}"),
    )

    with caplog.at_level(logging.WARNING, logger="x_digest.scheduler"):
        _run_startup_recovery(
            recovery_services,
            ("failing-list", list_id),
            ("daily",),
        )

    assert events == ["sync:failing-list", f"sync:{list_id}", "digest:daily"]
    assert "provider-secret-must-not-leak" not in caplog.text


def test_digest_job_runs_offline_digest_and_logs_digest_id(caplog) -> None:
    services, _ = make_services()
    with services.session_factory.begin() as session:
        author = Author(platform_author_id="author-a", username="alice")
        post = Post(
            platform="x",
            platform_post_id="source-a",
            author=author,
            text="offline digest item",
            created_at=datetime.now(UTC) - timedelta(days=1),
            source_url="https://x.com/alice/status/source-a",
        )
        session.add(post)
        session.flush()
        session.add(
            Summary(
                content_fingerprint="summary-a",
                model="fake-summary-v1",
                prompt_version="single-content-v1",
                generation=1,
                summary="offline digest item",
                key_points=["point"],
                topics=["ai"],
                importance=3,
                language="en",
                source_ids=[post.id],
            )
        )
    services = PipelineSchedulerServices(
        session_factory=services.session_factory,
        pipeline_runner=services.pipeline_runner,
        digest_runner=lambda cadence: run_offline_digest(
            services.session_factory,
            cadence,
            "UTC",
            DeterministicDigestSynthesizer(),
        ),
    )

    with caplog.at_level(logging.INFO, logger="x_digest.scheduler"):
        _run_digest(services, "daily")

    with services.session_factory() as session:
        digest = session.query(Digest).one()
        assert digest.rendered_content["schema_version"] == "event-digest-v1"
    event = next(json.loads(record.message) for record in caplog.records)
    assert event["event"] == "digest.completed"
    assert event["digest_id"]
    assert event["duration_ms"] >= 0
    assert event["counts"]["items"] == 1


def test_notification_job_uses_stable_id() -> None:
    services, _ = make_services()
    scheduler = build_scheduler(Settings(), services)

    schedule_notification(scheduler, services, "digest-123")

    assert scheduler.get_job("notify:digest-123") is not None


def test_completed_digest_triggers_notification_runner() -> None:
    services, _ = make_services()
    delivered: list[str] = []
    services = PipelineSchedulerServices(
        session_factory=services.session_factory,
        pipeline_runner=services.pipeline_runner,
        digest_runner=lambda _cadence: DigestRunResult(
            digest_id="digest-123", item_count=0
        ),
        notification_runner=lambda digest_id: delivered.append(digest_id),
    )

    _run_digest(services, "daily")

    assert delivered == ["digest-123"]


def test_scheduler_registers_notification_retry_reconciliation() -> None:
    services, _ = make_services()
    services = PipelineSchedulerServices(
        session_factory=services.session_factory,
        pipeline_runner=services.pipeline_runner,
        notification_retry_runner=lambda: None,
    )

    scheduler = build_scheduler(Settings(), services)

    job = scheduler.get_job("notification-retries")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 60
