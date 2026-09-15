from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import TracebackType

import httpx
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from x_digest.services.oauth_tokens import OAuthTokenSet, TokenVault

X_PROVIDER = "x"
DEFAULT_TOKEN_URL = "https://api.x.com/2/oauth2/token"
_PROCESS_LOCK_RETRY_SECONDS = 0.01


class _ProcessAsyncLock:
    """Serialize coroutines across event loops without blocking their threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    async def acquire(self) -> None:
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(_PROCESS_LOCK_RETRY_SECONDS)

    def release(self) -> None:
        self._lock.release()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()


_PROCESS_REFRESH_LOCK = _ProcessAsyncLock()
_PROCESS_CREDENTIAL_WRITE_LOCK = _ProcessAsyncLock()

type Clock = Callable[[], datetime]
type OAuthCredentialView = OAuthTokenSet


class OAuthClientError(Exception):
    """An OAuth failure safe to translate to the private API contract."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class XOAuthClient:
    """Exchange and refresh OAuth credentials without exposing provider details."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        client_id: str | None,
        redirect_uri: object | None,
        encryption_key: SecretStr | None,
        http_client: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
        token_url: str = DEFAULT_TOKEN_URL,
    ) -> None:
        self._session_factory = session_factory
        self._client_id = client_id
        self._redirect_uri = str(redirect_uri) if redirect_uri is not None else None
        self._encryption_key = encryption_key
        self._http_client = http_client or httpx.AsyncClient(timeout=10.0)
        self._owns_http_client = http_client is None
        self._clock = clock or _utc_now
        self._token_url = token_url

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthCredentialView:
        client_id, redirect_uri = self._configuration()
        payload = await self._request_tokens(
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
        )
        async with _PROCESS_CREDENTIAL_WRITE_LOCK:
            with self._session_factory.begin() as session:
                vault = self._vault(session)
                self._store(vault, payload)
                credential = self._load(vault)
        if credential is None:  # pragma: no cover - store guarantees a row
            raise _invalid_token_error()
        return credential

    async def access_token(self) -> str:
        self._configuration()
        with self._session_factory() as session:
            vault = self._vault(session)
            credential = self._load(vault)
            if credential is None:
                raise _authorization_error()
            if not self._needs_refresh(vault):
                return credential.access_token

        async with _PROCESS_REFRESH_LOCK:
            async with _PROCESS_CREDENTIAL_WRITE_LOCK:
                with self._session_factory.begin() as session:
                    vault = self._vault(session)
                    credential = self._load(vault)
                    if credential is None:
                        raise _authorization_error()
                    if not self._needs_refresh(vault):
                        return credential.access_token

            client_id, _redirect_uri = self._configuration()
            payload = await self._request_tokens(
                {
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": credential.refresh_token,
                }
            )

            async with _PROCESS_CREDENTIAL_WRITE_LOCK:
                with self._session_factory.begin() as session:
                    vault = self._vault(session)
                    current = self._load(vault)
                    if current is None:
                        raise _authorization_error()
                    if current != credential or not self._needs_refresh(vault):
                        return current.access_token
                    self._store(vault, payload, preserve_refresh_token_if_missing=True)
                    refreshed = self._load(vault)
                    if refreshed is None:  # pragma: no cover - store guarantees a row
                        raise _invalid_token_error()
                    return refreshed.access_token

    def _configuration(self) -> tuple[str, str]:
        if not self._client_id or not self._redirect_uri or self._encryption_key is None:
            raise OAuthClientError(503, "oauth_not_configured", "X OAuth is not configured")
        return self._client_id, self._redirect_uri

    def _vault(self, session: Session) -> TokenVault:
        try:
            return TokenVault(session=session, encryption_key=self._encryption_key)
        except ValueError:
            raise OAuthClientError(
                503,
                "oauth_not_configured",
                "X OAuth is not configured",
            ) from None

    def _needs_refresh(self, vault: TokenVault) -> bool:
        try:
            return vault.needs_refresh(provider=X_PROVIDER, now=self._clock())
        except ValueError:
            raise _invalid_token_error() from None

    @staticmethod
    def _load(vault: TokenVault) -> OAuthCredentialView | None:
        try:
            return vault.load(provider=X_PROVIDER)
        except ValueError:
            raise _invalid_token_error() from None

    def _store(
        self,
        vault: TokenVault,
        payload: Mapping[str, object],
        *,
        preserve_refresh_token_if_missing: bool = False,
    ) -> None:
        try:
            vault.store(
                provider=X_PROVIDER,
                token_payload=payload,
                now=self._clock(),
                preserve_refresh_token_if_missing=preserve_refresh_token_if_missing,
            )
        except ValueError:
            raise _invalid_token_error() from None

    async def _request_tokens(self, form: Mapping[str, str]) -> Mapping[str, object]:
        try:
            response = await self._http_client.post(self._token_url, data=form)
        except httpx.RequestError:
            raise _upstream_error() from None

        if response.status_code in {400, 401, 403}:
            raise _authorization_error()
        if response.status_code >= 500 or not 200 <= response.status_code < 300:
            raise _upstream_error()
        try:
            payload = response.json()
        except ValueError:
            raise _invalid_token_error() from None
        if not isinstance(payload, Mapping):
            raise _invalid_token_error()
        return payload


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _authorization_error() -> OAuthClientError:
    return OAuthClientError(400, "oauth_authorization_failed", "X authorization failed")


def _upstream_error() -> OAuthClientError:
    return OAuthClientError(503, "oauth_upstream_unavailable", "X OAuth is unavailable")


def _invalid_token_error() -> OAuthClientError:
    return OAuthClientError(502, "oauth_token_invalid", "X returned an invalid token")
