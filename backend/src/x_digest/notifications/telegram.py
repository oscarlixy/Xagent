import httpx

from x_digest.notifications.base import (
    DigestMessage,
    PermanentNotificationError,
    RetryableNotificationError,
)


class TelegramNotifier:
    def __init__(
        self, *, token: str, chat_id: str, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self._endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._http_client = http_client

    async def send(self, message: DigestMessage) -> str | None:
        text = f"{message.subject}\n\n{message.body}\n\n{message.url}\n{message.digest_id}"
        try:
            if self._http_client is not None:
                response = await self._http_client.post(
                    self._endpoint, json={"chat_id": self._chat_id, "text": text}, timeout=10
                )
            else:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(
                        self._endpoint, json={"chat_id": self._chat_id, "text": text}
                    )
        except httpx.HTTPError as error:
            raise RetryableNotificationError from error
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableNotificationError
        if not response.is_success:
            raise PermanentNotificationError
        try:
            message_id = response.json()["result"]["message_id"]
        except (KeyError, TypeError, ValueError):
            return None
        return str(message_id)
