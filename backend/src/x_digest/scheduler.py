from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler  # type: ignore[import-untyped]
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from x_digest.config import Settings
from x_digest.models import XList
from x_digest.services.ingestion import IngestionService
from x_digest.services.pipeline import PipelineService
from x_digest.services.summarization import DeterministicSummarizer
from x_digest.sources.fake import FakeXSource

MISFIRE_GRACE_SECONDS = 300
RECONCILE_INTERVAL_MINUTES = 5


@dataclass(frozen=True)
class PipelineSchedulerServices:
    session_factory: sessionmaker[Session]
    pipeline_runner: Callable[[str], Any]
    digest_runner: Callable[[str], Any] | None = None
    notification_runner: Callable[[str], Any] | None = None


def build_scheduler(settings: Settings, services: PipelineSchedulerServices) -> BlockingScheduler:
    timezone = ZoneInfo(settings.app_timezone)
    scheduler = BlockingScheduler(timezone=timezone)
    reconcile_list_jobs(scheduler, services)
    scheduler.add_job(
        reconcile_list_jobs,
        trigger="interval",
        minutes=RECONCILE_INTERVAL_MINUTES,
        args=[scheduler, services],
        id="reconcile-lists",
        replace_existing=True,
        **_safe_job_options(),
    )
    for cadence in _configured_cadences(settings.digest_cadence):
        if cadence == "6h":
            trigger: Any = "interval"
            trigger_kwargs = {"hours": 6, "timezone": timezone}
        else:
            trigger = "cron"
            trigger_kwargs = {"hour": 8, "minute": 0, "timezone": timezone}
        scheduler.add_job(
            _run_digest,
            trigger=trigger,
            args=[services, cadence],
            id=f"digest:{cadence}",
            replace_existing=True,
            **trigger_kwargs,
            **_safe_job_options(),
        )
    return scheduler


def reconcile_list_jobs(scheduler: BlockingScheduler, services: PipelineSchedulerServices) -> None:
    with services.session_factory() as session:
        enabled_lists = session.scalars(select(XList).where(XList.enabled.is_(True))).all()
    enabled_ids = {x_list.id for x_list in enabled_lists}
    for job in scheduler.get_jobs():
        if job.id.startswith("sync-list:") and job.id.removeprefix("sync-list:") not in enabled_ids:
            scheduler.remove_job(job.id)
    for x_list in enabled_lists:
        _remove_pending_job(scheduler, f"sync-list:{x_list.id}")
        scheduler.add_job(
            _run_pipeline,
            trigger="interval",
            minutes=x_list.sync_interval_minutes,
            args=[services, x_list.id],
            id=f"sync-list:{x_list.id}",
            replace_existing=True,
            **_safe_job_options(),
        )


def schedule_notification(
    scheduler: BlockingScheduler, services: PipelineSchedulerServices, digest_id: str
) -> None:
    scheduler.add_job(
        _run_notification,
        trigger="date",
        args=[services, digest_id],
        id=f"notify:{digest_id}",
        replace_existing=True,
        **_safe_job_options(),
    )


def _configured_cadences(cadence: str) -> tuple[str, ...]:
    if cadence == "both":
        return ("6h", "daily")
    return (cadence,)


def _safe_job_options() -> dict[str, Any]:
    return {
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": MISFIRE_GRACE_SECONDS,
    }


def _remove_pending_job(scheduler: BlockingScheduler, job_id: str) -> None:
    """Make reconciliation effective before a foreground scheduler is started.

    APScheduler holds pre-start jobs in a pending list, where replacement is deferred
    until ``start()``. Removing the prior pending job keeps an interval edit visible
    immediately while retaining ``replace_existing=True`` for a running scheduler.
    """

    if scheduler.get_job(job_id) is not None:
        scheduler.remove_job(job_id)


def _run_pipeline(services: PipelineSchedulerServices, list_id: str) -> None:
    _run_maybe_async(services.pipeline_runner(list_id))


def _run_digest(services: PipelineSchedulerServices, cadence: str) -> None:
    if services.digest_runner is not None:
        _run_maybe_async(services.digest_runner(cadence))


def _run_notification(services: PipelineSchedulerServices, digest_id: str) -> None:
    if services.notification_runner is not None:
        _run_maybe_async(services.notification_runner(digest_id))


def _run_maybe_async(value: Any) -> None:
    if inspect.isawaitable(value):
        asyncio.run(_await(value))


async def _await(value: Any) -> None:
    await value


def main() -> None:
    """Run the scheduler as a dedicated foreground process."""

    settings = Settings()
    engine = create_engine(settings.database_url)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    pipeline = PipelineService(
        ingestion=IngestionService(
            session_factory=session_factory,
            source=FakeXSource({}),
            max_pages=settings.x_max_pages_per_sync,
            max_posts=settings.x_max_posts_per_sync,
        ),
        session_factory=session_factory,
        summarizer=DeterministicSummarizer(),
        llm_max_items_per_run=settings.llm_max_items_per_run,
    )
    build_scheduler(
        settings,
        PipelineSchedulerServices(
            session_factory=session_factory,
            pipeline_runner=pipeline.run_list_pipeline,
        ),
    ).start()


if __name__ == "__main__":  # pragma: no cover
    main()
