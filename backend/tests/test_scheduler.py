import json
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from x_digest.config import Settings
from x_digest.models import Base, Digest, Summary, XList
from x_digest.repositories.lists import upsert_list
from x_digest.scheduler import (
    PipelineSchedulerServices,
    _run_digest,
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


def test_scheduler_adds_enabled_list_job_with_safe_execution_defaults() -> None:
    services, list_id = make_services()

    scheduler = build_scheduler(Settings(app_timezone="UTC", digest_cadence="daily"), services)

    job = scheduler.get_job(f"sync-list:{list_id}")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 15 * 60
    assert job.coalesce is True
    assert job.max_instances == 1
    assert job.misfire_grace_time == 300


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


def test_digest_job_runs_offline_digest_and_logs_digest_id(caplog) -> None:
    services, _ = make_services()
    with services.session_factory.begin() as session:
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
                source_ids=["source-a"],
            )
        )
    services = PipelineSchedulerServices(
        session_factory=services.session_factory,
        pipeline_runner=services.pipeline_runner,
        digest_runner=lambda cadence: run_offline_digest(
            services.session_factory, cadence, "UTC"
        ),
    )

    with caplog.at_level(logging.INFO, logger="x_digest.scheduler"):
        _run_digest(services, "daily")

    with services.session_factory() as session:
        assert len(session.query(Digest).all()) == 1
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
