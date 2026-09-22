import asyncio
import smtplib
from email.message import EmailMessage

from x_digest.notifications.base import (
    DigestMessage,
    PermanentNotificationError,
    RetryableNotificationError,
)


class SMTPNotifier:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        recipient: str,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._recipient = recipient
        self._username = username
        self._password = password
        self._starttls = starttls

    async def send(self, message: DigestMessage) -> str | None:
        return await asyncio.to_thread(self._send, message)

    def _send(self, message: DigestMessage) -> str | None:
        email = EmailMessage()
        email["From"] = self._sender
        email["To"] = self._recipient
        email["Subject"] = message.subject
        email.set_content(f"{message.body}\n\n{message.url}\nDigest ID: {message.digest_id}")
        try:
            with smtplib.SMTP(self._host, self._port, timeout=10) as client:
                if self._starttls:
                    client.starttls()
                if self._username:
                    client.login(self._username, self._password or "")
                client.send_message(email)
        except smtplib.SMTPAuthenticationError as error:
            raise PermanentNotificationError from error
        except smtplib.SMTPResponseException as error:
            exception = (
                RetryableNotificationError
                if 400 <= error.smtp_code < 500
                else PermanentNotificationError
            )
            raise exception from error
        except (OSError, smtplib.SMTPException) as error:
            raise RetryableNotificationError from error
        return None
