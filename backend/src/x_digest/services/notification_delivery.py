from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from x_digest.models import Digest, NotificationDelivery
from x_digest.notifications.base import (
    DigestMessage,
    Notifier,
    PermanentNotificationError,
    RetryableNotificationError,
)


class NotificationDeliveryService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        notifiers: dict[str, Notifier],
        frontend_url: str,
        max_attempts: int = 3,
        retry_delay: timedelta = timedelta(minutes=5),
    ) -> None:
        self._session_factory = session_factory
        self._notifiers = notifiers
        self._frontend_url = frontend_url.rstrip("/")
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay

    def enqueue(self, digest_id: str) -> list[str]:
        ids: list[str] = []
        with self._session_factory.begin() as session:
            for channel in sorted(self._notifiers):
                row = session.scalar(
                    select(NotificationDelivery).where(
                        NotificationDelivery.digest_id == digest_id,
                        NotificationDelivery.channel == channel,
                    )
                )
                if row is None:
                    try:
                        with session.begin_nested():
                            row = NotificationDelivery(
                                digest_id=digest_id, channel=channel, status="pending"
                            )
                            session.add(row)
                            session.flush()
                    except IntegrityError:
                        row = session.scalar(
                            select(NotificationDelivery).where(
                                NotificationDelivery.digest_id == digest_id,
                                NotificationDelivery.channel == channel,
                            )
                        )
                if row is not None:
                    ids.append(row.id)
        return ids

    def due_ids(self, *, limit: int = 100) -> list[str]:
        now = datetime.now(UTC)
        stale = now - timedelta(minutes=15)
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(NotificationDelivery.id)
                    .where(
                        NotificationDelivery.attempt_count < self._max_attempts,
                        or_(
                            and_(
                                NotificationDelivery.status.in_(("pending", "retry")),
                                or_(
                                    NotificationDelivery.next_attempt_at.is_(None),
                                    NotificationDelivery.next_attempt_at <= now,
                                ),
                            ),
                            and_(
                                NotificationDelivery.status == "sending",
                                NotificationDelivery.updated_at <= stale,
                            ),
                        ),
                    )
                    .order_by(NotificationDelivery.created_at, NotificationDelivery.id)
                    .limit(limit)
                )
            )

    async def deliver(self, delivery_id: str) -> bool:
        now = datetime.now(UTC)
        stale = now - timedelta(minutes=15)
        with self._session_factory.begin() as session:
            claimed = session.execute(
                update(NotificationDelivery)
                .where(
                    NotificationDelivery.id == delivery_id,
                    NotificationDelivery.attempt_count < self._max_attempts,
                    or_(
                        and_(
                            NotificationDelivery.status.in_(("pending", "retry")),
                            or_(
                                NotificationDelivery.next_attempt_at.is_(None),
                                NotificationDelivery.next_attempt_at <= now,
                            ),
                        ),
                        and_(
                            NotificationDelivery.status == "sending",
                            NotificationDelivery.updated_at <= stale,
                        ),
                    ),
                )
                .values(
                    status="sending",
                    attempt_count=NotificationDelivery.attempt_count + 1,
                    updated_at=now,
                )
            )
            if getattr(claimed, "rowcount", 0) != 1:
                return False
            delivery = session.get(NotificationDelivery, delivery_id)
            assert delivery is not None
            digest = session.get(Digest, delivery.digest_id)
            if digest is None:
                delivery.status = "failed"
                return False
            channel = delivery.channel
            attempt_count = delivery.attempt_count
            message = self._message(digest)

        notifier = self._notifiers.get(channel)
        if notifier is None:
            error: Exception | None = PermanentNotificationError()
            receipt = None
        else:
            try:
                receipt = await notifier.send(message)
                error = None
            except (RetryableNotificationError, PermanentNotificationError) as caught:
                error = caught
                receipt = None
            except Exception:
                error = RetryableNotificationError()
                receipt = None

        with self._session_factory.begin() as session:
            delivery = session.get(NotificationDelivery, delivery_id)
            if delivery is None:
                return False
            if error is None:
                delivery.status = "succeeded"
                delivery.provider_receipt = receipt
                delivery.next_attempt_at = None
                return True
            if isinstance(error, RetryableNotificationError) and attempt_count < self._max_attempts:
                delivery.status = "retry"
                delivery.next_attempt_at = datetime.now(UTC) + self._retry_delay * (
                    2 ** (attempt_count - 1)
                )
            else:
                delivery.status = "failed"
                delivery.next_attempt_at = None
            return False

    def _message(self, digest: Digest) -> DigestMessage:
        rendered = digest.rendered_content or {}
        subject = str(rendered.get("title") or "X Digest")
        body = str(rendered.get("overview") or rendered.get("summary") or "Digest ready")
        return DigestMessage(
            digest_id=digest.id,
            subject=subject,
            body=body,
            url=f"{self._frontend_url}/digests/{digest.id}",
        )
