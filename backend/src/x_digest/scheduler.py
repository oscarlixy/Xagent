from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler  # type: ignore[import-untyped]
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from x_digest.ai.providers import build_digest_synthesizer, build_summarizer
from x_digest.config import Settings
from x_digest.models import DigestItem, XList
from x_digest.notifications.base import Notifier
from x_digest.notifications.email import SMTPNotifier
from x_digest.notifications.telegram import TelegramNotifier
from x_digest.services.digest_synthesis import DigestSynthesizer
from x_digest.services.digests import generate_digest, window_for
from x_digest.services.ingestion import IngestionService
from x_digest.services.link_content import LinkContentFetcher
from x_digest.services.notification_delivery import NotificationDeliveryService
from x_digest.services.oauth_client import XOAuthClient
from x_digest.services.pipeline import PipelineResult, PipelineService
from x_digest.sources.x_api import XApiSource

RECONCILE_INTERVAL_MINUTES = 5
logger = logging.getLogger(__name__)

type HttpClientFactory = Callable[[], httpx.AsyncClient]
type ProductionPipelineRunner = Callable[[str], Awaitable[PipelineResult]]


@dataclass(frozen=True)
class PipelineSchedulerServices:
    session_factory: sessionmaker[Session]
    pipeline_runner: Callable[[str], Any]
    digest_runner: Callable[[str], Any] | None = None
    notification_runner: Callable[[str], Any] | None = None
    notification_retry_runner: Callable[[], Any] | None = None


@dataclass(frozen=True)
class DigestRunResult:
    digest_id: str
    item_count: int


def build_scheduler(settings: Settings, services: PipelineSchedulerServices) -> BlockingScheduler:
    timezone = ZoneInfo(settings.app_timezone)
    scheduler = BlockingScheduler(timezone=timezone)
    reconcile_list_jobs(scheduler, services, run_new_jobs_immediately=False)
    scheduler.add_job(
        reconcile_list_jobs,
        trigger="interval",
        minutes=RECONCILE_INTERVAL_MINUTES,
        args=[scheduler, services],
        id="reconcile-lists",
        replace_existing=True,
        **_safe_job_options(),
    )
    cadences = _configured_cadences(settings.digest_cadence)
    for cadence in cadences:
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
    enabled_list_ids = _enabled_list_ids(services)
    scheduler.add_job(
        _run_startup_recovery,
        trigger="date",
        run_date=datetime.now(timezone),
        args=[services, enabled_list_ids, cadences],
        id="startup-recovery",
        replace_existing=True,
        **_safe_job_options(),
    )
    if services.notification_retry_runner is not None:
        scheduler.add_job(
            _run_notification_retries,
            trigger="interval",
            minutes=1,
            args=[services],
            id="notification-retries",
            replace_existing=True,
            **_safe_job_options(),
        )
    return scheduler


def reconcile_list_jobs(
    scheduler: BlockingScheduler,
    services: PipelineSchedulerServices,
    *,
    run_new_jobs_immediately: bool = True,
) -> None:
    with services.session_factory() as session:
        enabled_lists = session.scalars(select(XList).where(XList.enabled.is_(True))).all()
    enabled_ids = {x_list.id for x_list in enabled_lists}
    for job in scheduler.get_jobs():
        if job.id.startswith("sync-list:") and job.id.removeprefix("sync-list:") not in enabled_ids:
            scheduler.remove_job(job.id)
    for x_list in enabled_lists:
        job_id = f"sync-list:{x_list.id}"
        existing = scheduler.get_job(job_id)
        if existing is not None and _has_interval(existing, x_list.sync_interval_minutes):
            continue
        if existing is not None:
            if scheduler.running:
                scheduler.reschedule_job(
                    job_id, trigger="interval", minutes=x_list.sync_interval_minutes
                )
                continue
            scheduler.remove_job(job_id)
        options = _safe_job_options()
        if run_new_jobs_immediately:
            options["next_run_time"] = datetime.now(scheduler.timezone)
        scheduler.add_job(
            _run_pipeline,
            trigger="interval",
            minutes=x_list.sync_interval_minutes,
            args=[services, x_list.id],
            id=job_id,
            replace_existing=True,
            **options,
        )


def _enabled_list_ids(services: PipelineSchedulerServices) -> tuple[str, ...]:
    with services.session_factory() as session:
        return tuple(
            session.scalars(
                select(XList.id).where(XList.enabled.is_(True)).order_by(XList.id)
            ).all()
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
        "misfire_grace_time": None,
    }


def _has_interval(job: Any, minutes: int) -> bool:
    return getattr(job.trigger, "interval", None) == timedelta(minutes=minutes)


def _run_pipeline(services: PipelineSchedulerServices, list_id: str) -> None:
    _run_maybe_async(services.pipeline_runner(list_id))


def _run_startup_recovery(
    services: PipelineSchedulerServices,
    list_ids: tuple[str, ...],
    cadences: tuple[str, ...],
) -> None:
    for list_id in list_ids:
        try:
            _run_pipeline(services, list_id)
        except Exception:
            logger.warning(
                json.dumps(
                    {
                        "event": "pipeline.startup_recovery_failed",
                        "list_id": list_id,
                        "error_code": "pipeline_processing_failed",
                    }
                )
            )
    for cadence in cadences:
        _run_digest(services, cadence)


def _run_digest(services: PipelineSchedulerServices, cadence: str) -> None:
    if services.digest_runner is None:
        return
    started = time.monotonic()
    try:
        result = _run_maybe_async(services.digest_runner(cadence))
    except Exception:
        logger.warning(
            json.dumps(
                {
                    "event": "digest.failed",
                    "digest_id": None,
                    "duration_ms": _elapsed_ms(started),
                    "counts": {"items": 0},
                    "error_code": "digest_processing_failed",
                }
            )
        )
        return
    if isinstance(result, DigestRunResult):
        logger.info(
            json.dumps(
                {
                    "event": "digest.completed",
                    "digest_id": result.digest_id,
                    "duration_ms": _elapsed_ms(started),
                    "counts": {"items": result.item_count},
                    "error_code": None,
                }
            )
        )
        _run_notification(services, result.digest_id)


def _run_notification(services: PipelineSchedulerServices, digest_id: str) -> None:
    if services.notification_runner is not None:
        _run_maybe_async(services.notification_runner(digest_id))


def _run_notification_retries(services: PipelineSchedulerServices) -> None:
    if services.notification_retry_runner is not None:
        _run_maybe_async(services.notification_retry_runner())


def _run_maybe_async(value: Any) -> Any:
    if inspect.isawaitable(value):
        return asyncio.run(_await(value))
    return value


async def _await(value: Any) -> Any:
    return await value


async def run_offline_digest(
    session_factory: sessionmaker[Session],
    cadence: str,
    timezone: str,
    synthesizer: DigestSynthesizer,
) -> DigestRunResult:
    if cadence not in ("6h", "daily"):
        raise ValueError(f"Unsupported digest cadence: {cadence}")
    typed_cadence = cast(Literal["6h", "daily"], cadence)
    window = window_for(datetime.now(ZoneInfo(timezone)), typed_cadence, ZoneInfo(timezone))
    with session_factory.begin() as session:
        digest, _ = await generate_digest(
            session,
            window=window,
            synthesizer=synthesizer,
        )
        item_count = session.scalar(
            select(func.count()).select_from(DigestItem).where(DigestItem.digest_id == digest.id)
        )
        return DigestRunResult(digest_id=digest.id, item_count=int(item_count or 0))


def _digest_window_key(cadence: str, timezone: str) -> str:
    if cadence not in ("6h", "daily"):
        raise ValueError(f"Unsupported digest cadence: {cadence}")
    typed_cadence = cast(Literal["6h", "daily"], cadence)
    return window_for(datetime.now(ZoneInfo(timezone)), typed_cadence, ZoneInfo(timezone)).key


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def build_production_pipeline(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    oauth_http_client: httpx.AsyncClient | None = None,
    x_api_http_client: httpx.AsyncClient | None = None,
) -> tuple[PipelineService, XOAuthClient, XApiSource]:
    """Create the scheduler pipeline with the OAuth-backed production source."""

    oauth_client = XOAuthClient(
        session_factory=session_factory,
        client_id=settings.x_client_id,
        client_secret=settings.x_client_secret,
        redirect_uri=settings.x_oauth_redirect_uri,
        encryption_key=settings.x_token_encryption_key,
        http_client=oauth_http_client,
    )
    source = XApiSource(
        token_service=oauth_client,
        http_client=x_api_http_client,
        source_mode=settings.x_source_mode,
    )
    return (
        PipelineService(
            ingestion=IngestionService(
                session_factory=session_factory,
                source=source,
                max_pages=settings.x_max_pages_per_sync,
                max_posts=settings.x_max_posts_per_sync,
                max_posts_per_day=settings.x_max_posts_per_day,
                page_size=settings.x_page_size,
                overlap=timedelta(minutes=settings.x_ingestion_overlap_minutes),
                backfill_window=timedelta(hours=settings.x_ingestion_backfill_hours),
            ),
            session_factory=session_factory,
            summarizer=build_summarizer(settings),
            llm_max_items_per_run=settings.llm_max_items_per_run,
            link_fetcher=LinkContentFetcher(),
            link_max_items_per_run=settings.link_max_items_per_run,
            link_max_attempts=settings.link_max_attempts,
            link_retry_delay=timedelta(seconds=settings.link_retry_base_seconds),
        ),
        oauth_client,
        source,
    )


def build_production_pipeline_runner(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    oauth_http_client_factory: HttpClientFactory | None = None,
    x_api_http_client_factory: HttpClientFactory | None = None,
) -> ProductionPipelineRunner:
    """Build a runner whose provider clients belong to one scheduled invocation."""

    oauth_factory = oauth_http_client_factory or _new_provider_http_client
    api_factory = x_api_http_client_factory or _new_provider_http_client

    async def run(list_id: str) -> PipelineResult:
        oauth_http_client = oauth_factory()
        try:
            x_api_http_client = api_factory()
            try:
                pipeline, _oauth_client, _source = build_production_pipeline(
                    settings,
                    session_factory,
                    oauth_http_client=oauth_http_client,
                    x_api_http_client=x_api_http_client,
                )
                return await pipeline.run_list_pipeline(list_id)
            finally:
                await x_api_http_client.aclose()
        finally:
            await oauth_http_client.aclose()

    return run


def _new_provider_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0)


def build_notification_runner(
    settings: Settings, session_factory: sessionmaker[Session]
) -> tuple[Callable[[str], Awaitable[None]], Callable[[], Awaitable[None]]] | None:
    notifiers: dict[str, Notifier] = {}
    if settings.smtp_host and settings.smtp_from and settings.smtp_to:
        notifiers["email"] = SMTPNotifier(
            host=settings.smtp_host,
            port=settings.smtp_port,
            sender=settings.smtp_from,
            recipient=settings.smtp_to,
            username=settings.smtp_username,
            password=(
                settings.smtp_password.get_secret_value() if settings.smtp_password else None
            ),
            starttls=settings.smtp_starttls,
        )
    if settings.telegram_bot_token and settings.telegram_chat_id:
        notifiers["telegram"] = TelegramNotifier(
            token=settings.telegram_bot_token.get_secret_value(),
            chat_id=settings.telegram_chat_id,
        )
    if not notifiers:
        return None
    service = NotificationDeliveryService(
        session_factory=session_factory,
        notifiers=notifiers,
        frontend_url=str(settings.notification_frontend_url).rstrip("/"),
        max_attempts=settings.notification_max_attempts,
        retry_delay=timedelta(seconds=settings.notification_retry_seconds),
    )

    async def run(digest_id: str) -> None:
        for delivery_id in service.enqueue(digest_id):
            await service.deliver(delivery_id)

    async def retry_due() -> None:
        for delivery_id in service.due_ids():
            await service.deliver(delivery_id)

    return run, retry_due


def main() -> None:
    """Run the scheduler as a dedicated foreground process."""

    settings = Settings()
    engine = create_engine(settings.database_url)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    notification_runners = build_notification_runner(settings, session_factory)
    digest_synthesizer = build_digest_synthesizer(settings)
    build_scheduler(
        settings,
        PipelineSchedulerServices(
            session_factory=session_factory,
            pipeline_runner=build_production_pipeline_runner(settings, session_factory),
            digest_runner=lambda cadence: run_offline_digest(
                session_factory,
                cadence,
                settings.app_timezone,
                digest_synthesizer,
            ),
            notification_runner=notification_runners[0] if notification_runners else None,
            notification_retry_runner=(
                notification_runners[1] if notification_runners else None
            ),
        ),
    ).start()


if __name__ == "__main__":  # pragma: no cover
    main()
