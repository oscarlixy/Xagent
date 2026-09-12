from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from x_digest.models import Base, OAuthCredential
from x_digest.services.oauth_tokens import OAuthTokenSet, TokenVault

FERNET_KEY = SecretStr("MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
NOW = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session


def token_payload(
    *, access_token: str = "test-access-alpha", refresh_token: str = "test-refresh-alpha"
) -> dict[str, object]:
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": 3600,
        "scope": "tweet.read users.read list.read offline.access",
    }


def test_fernet_round_trip_returns_decrypted_tokens_only_on_load(session: Session) -> None:
    vault = TokenVault(session=session, encryption_key=FERNET_KEY)

    vault.store(provider="x", token_payload=token_payload(), now=NOW)

    assert vault.load(provider="x") == OAuthTokenSet(
        access_token="test-access-alpha",
        refresh_token="test-refresh-alpha",
        access_expires_at=NOW + timedelta(hours=1),
        scope="tweet.read users.read list.read offline.access",
    )


@pytest.mark.parametrize("key", [None, SecretStr("not-a-fernet-key")])
def test_missing_or_invalid_encryption_key_is_rejected(
    session: Session, key: SecretStr | None
) -> None:
    with pytest.raises(ValueError):
        TokenVault(session=session, encryption_key=key)


def test_persisted_values_do_not_contain_plaintext_tokens(session: Session) -> None:
    vault = TokenVault(session=session, encryption_key=FERNET_KEY)
    vault.store(provider="x", token_payload=token_payload(), now=NOW)

    credential = session.scalar(select(OAuthCredential).where(OAuthCredential.provider == "x"))

    assert credential is not None
    assert b"test-access-alpha" not in credential.encrypted_access_token
    assert b"test-refresh-alpha" not in credential.encrypted_refresh_token


def test_store_overwrites_the_single_credential_for_a_provider(session: Session) -> None:
    vault = TokenVault(session=session, encryption_key=FERNET_KEY)
    vault.store(provider="x", token_payload=token_payload(), now=NOW)

    vault.store(
        provider="x",
        token_payload=token_payload(
            access_token="test-access-beta", refresh_token="test-refresh-beta"
        ),
        now=NOW + timedelta(minutes=5),
    )

    assert session.scalar(select(func.count()).select_from(OAuthCredential)) == 1
    assert vault.load(provider="x") == OAuthTokenSet(
        access_token="test-access-beta",
        refresh_token="test-refresh-beta",
        access_expires_at=NOW + timedelta(minutes=65),
        scope="tweet.read users.read list.read offline.access",
    )


def test_expiry_uses_utc_and_refreshes_at_the_sixty_second_margin(session: Session) -> None:
    vault = TokenVault(session=session, encryption_key=FERNET_KEY)
    vault.store(provider="x", token_payload=token_payload(), now=NOW)

    token_set = vault.load(provider="x")
    assert token_set is not None
    assert token_set.access_expires_at == NOW + timedelta(hours=1)
    assert token_set.access_expires_at.tzinfo is UTC
    assert not vault.needs_refresh(provider="x", now=NOW + timedelta(minutes=58, seconds=59))
    assert vault.needs_refresh(provider="x", now=NOW + timedelta(minutes=59))


def test_store_rejects_a_naive_now_value(session: Session) -> None:
    vault = TokenVault(session=session, encryption_key=FERNET_KEY)

    with pytest.raises(ValueError):
        vault.store(provider="x", token_payload=token_payload(), now=NOW.replace(tzinfo=None))


def test_needs_refresh_rejects_a_naive_now_value(session: Session) -> None:
    vault = TokenVault(session=session, encryption_key=FERNET_KEY)

    with pytest.raises(ValueError):
        vault.needs_refresh(provider="x", now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "payload",
    [
        {"refresh_token": "test-refresh", "expires_in": 3600},
        {"access_token": "test-access", "expires_in": 3600},
        {
            "access_token": "test-access",
            "refresh_token": "test-refresh",
            "expires_in": 0,
        },
    ],
)
def test_invalid_token_payload_is_rejected(session: Session, payload: dict[str, Any]) -> None:
    vault = TokenVault(session=session, encryption_key=FERNET_KEY)

    with pytest.raises(ValueError):
        vault.store(provider="x", token_payload=payload, now=NOW)
