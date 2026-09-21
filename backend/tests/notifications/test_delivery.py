import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from x_digest.models import Base, Digest, NotificationDelivery
from x_digest.notifications.base import (
    DigestMessage,
    PermanentNotificationError,
    RetryableNotificationError,
)
from x_digest.services.notification_delivery import NotificationDeliveryService


@dataclass
class FakeNotifier:
    error: Exception | None = None
    calls: int = 0
    messages: list[DigestMessage] = field(default_factory=list)

    async def send(self, message: DigestMessage) -> str | None:
        self.calls += 1
        self.messages.append(message)
        assert message.digest_id
        assert message.url.endswith(message.digest_id)
        if self.error is not None:
            raise self.error
        return "receipt-1"


def _service(notifiers: dict[str, FakeNotifier]):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        digest = Digest(
            window_key="window",
            version=1,
            rendered_content={
                "title": "Daily digest",
                "overview": "事件摘要概览",
                "summary": "Legacy summary",
            },
        )
        session.add(digest)
        session.flush()
        digest_id = digest.id
    service = NotificationDeliveryService(
        session_factory=factory,
        notifiers=notifiers,  # type: ignore[arg-type]
        frontend_url="https://digest.example.test",
        max_attempts=2,
        retry_delay=timedelta(0),
    )
    return factory, digest_id, service


def test_disabled_channels_create_no_delivery_rows() -> None:
    factory, digest_id, service = _service({})

    assert service.enqueue(digest_id) == []
    with factory() as session:
        assert session.scalars(select(NotificationDelivery)).all() == []


def test_enqueue_is_unique_and_successful_delivery_persists_receipt() -> None:
    notifier = FakeNotifier()
    factory, digest_id, service = _service({"telegram": notifier})

    first = service.enqueue(digest_id)
    second = service.enqueue(digest_id)
    delivered = asyncio.run(service.deliver(first[0]))

    assert second == first
    assert delivered is True
    assert notifier.calls == 1
    assert notifier.messages[0].body == "事件摘要概览"
    with factory() as session:
        row = session.get(NotificationDelivery, first[0])
        assert row is not None
        assert row.status == "succeeded"
        assert row.attempt_count == 1
        assert row.provider_receipt == "receipt-1"


def test_retryable_delivery_stops_at_attempt_limit() -> None:
    notifier = FakeNotifier(error=RetryableNotificationError())
    factory, digest_id, service = _service({"email": notifier})
    delivery_id = service.enqueue(digest_id)[0]

    assert asyncio.run(service.deliver(delivery_id)) is False
    assert asyncio.run(service.deliver(delivery_id)) is False
    assert asyncio.run(service.deliver(delivery_id)) is False

    assert notifier.calls == 2
    with factory() as session:
        row = session.get(NotificationDelivery, delivery_id)
        assert row is not None
        assert row.status == "failed"
        assert row.attempt_count == 2
        assert row.next_attempt_at is None


def test_permanent_delivery_failure_is_not_retried() -> None:
    notifier = FakeNotifier(error=PermanentNotificationError())
    factory, digest_id, service = _service({"email": notifier})
    delivery_id = service.enqueue(digest_id)[0]

    assert asyncio.run(service.deliver(delivery_id)) is False
    assert asyncio.run(service.deliver(delivery_id)) is False

    assert notifier.calls == 1
    with factory() as session:
        row = session.get(NotificationDelivery, delivery_id)
        assert row is not None
        assert row.status == "failed"
