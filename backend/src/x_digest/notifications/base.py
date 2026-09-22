from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DigestMessage:
    digest_id: str
    subject: str
    body: str
    url: str


class RetryableNotificationError(RuntimeError):
    pass


class PermanentNotificationError(RuntimeError):
    pass


class Notifier(Protocol):
    async def send(self, message: DigestMessage) -> str | None: ...
