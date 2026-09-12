from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr
from sqlalchemy.orm import Session

from x_digest.models import OAuthCredential

REFRESH_MARGIN = timedelta(seconds=60)


@dataclass(frozen=True)
class OAuthTokenSet:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    scope: str


class TokenVault:
    """Encrypt and persist OAuth credentials without exposing stored ciphertext."""

    def __init__(self, *, session: Session, encryption_key: SecretStr | None) -> None:
        if encryption_key is None:
            raise ValueError("OAuth token encryption key is not configured")
        try:
            self._fernet = Fernet(encryption_key.get_secret_value().encode())
        except (TypeError, ValueError) as exc:
            raise ValueError("OAuth token encryption key is invalid") from exc
        self._session = session

    def store(
        self,
        *,
        provider: str,
        token_payload: Mapping[str, object],
        now: datetime,
    ) -> None:
        access_token, refresh_token, expires_in, scope = _validated_payload(token_payload)
        now_utc = _require_aware_utc(now)
        credential = self._session.get(OAuthCredential, provider)
        if credential is None:
            credential = OAuthCredential(
                provider=provider,
                encrypted_access_token=b"",
                encrypted_refresh_token=b"",
                access_expires_at=now_utc,
                scope="",
                created_at=now_utc,
                updated_at=now_utc,
            )
            self._session.add(credential)

        credential.encrypted_access_token = self._fernet.encrypt(access_token.encode())
        credential.encrypted_refresh_token = self._fernet.encrypt(refresh_token.encode())
        credential.access_expires_at = now_utc + timedelta(seconds=expires_in)
        credential.scope = scope
        credential.updated_at = now_utc
        self._session.flush()

    def load(self, *, provider: str) -> OAuthTokenSet | None:
        credential = self._session.get(OAuthCredential, provider)
        if credential is None:
            return None
        try:
            access_token = self._fernet.decrypt(credential.encrypted_access_token).decode()
            refresh_token = self._fernet.decrypt(credential.encrypted_refresh_token).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise ValueError("Stored OAuth credential cannot be decrypted") from exc
        return OAuthTokenSet(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=_persisted_utc(credential.access_expires_at),
            scope=credential.scope,
        )

    def needs_refresh(self, *, provider: str, now: datetime) -> bool:
        now_utc = _require_aware_utc(now)
        credential = self._session.get(OAuthCredential, provider)
        if credential is None:
            return True
        return _persisted_utc(credential.access_expires_at) <= now_utc + REFRESH_MARGIN


def _validated_payload(payload: Mapping[str, object]) -> tuple[str, str, int, str]:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    scope = payload.get("scope", "")
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(refresh_token, str)
        or not refresh_token
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or expires_in <= 0
        or not isinstance(scope, str)
    ):
        raise ValueError("Invalid OAuth token payload")
    return access_token, refresh_token, expires_in, scope


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("OAuth token time must include a timezone")
    return value.astimezone(UTC)


def _persisted_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
