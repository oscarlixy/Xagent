from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from x_digest.models import Base
from x_digest.repositories.jobs import advance_list_cursors, begin_sync_run, finish_sync_run
from x_digest.repositories.lists import upsert_list


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session


@pytest.mark.parametrize("status", ["succeeded", "partial"])
def test_safe_terminal_status_advances_observed_and_valid_cursors(
    session: Session, status: str
) -> None:
    x_list = upsert_list(session, platform_list_id="list-a", name="First")
    run = begin_sync_run(session, list_id=x_list.id)
    finish_sync_run(session, sync_run=run, status=status)

    advanced = advance_list_cursors(
        session,
        x_list=x_list,
        sync_run=run,
        latest_observed_post_id="head-2",
        latest_seen_at=datetime(2026, 9, 23, tzinfo=UTC),
        latest_seen_post_id="valid-2",
    )

    assert advanced == (True, True)
    assert x_list.latest_observed_post_id == "head-2"
    assert x_list.latest_seen_post_id == "valid-2"


def test_failed_run_advances_neither_cursor(session: Session) -> None:
    x_list = upsert_list(session, platform_list_id="list-a", name="First")
    x_list.latest_observed_post_id = "stored-head"
    run = begin_sync_run(session, list_id=x_list.id)
    finish_sync_run(session, sync_run=run, status="failed")

    advanced = advance_list_cursors(
        session,
        x_list=x_list,
        sync_run=run,
        latest_observed_post_id="new-head",
        latest_seen_at=datetime(2026, 9, 23, tzinfo=UTC),
        latest_seen_post_id="valid-2",
    )

    assert advanced == (False, False)
    assert x_list.latest_observed_post_id == "stored-head"
    assert x_list.latest_seen_at is None
    assert x_list.latest_seen_post_id is None


def test_cursor_helper_never_moves_valid_watermark_backward(session: Session) -> None:
    stored_at = datetime(2026, 9, 23, tzinfo=UTC)
    x_list = upsert_list(session, platform_list_id="list-a", name="First")
    x_list.latest_seen_at = stored_at
    x_list.latest_seen_post_id = "valid-9"
    run = begin_sync_run(session, list_id=x_list.id)
    finish_sync_run(session, sync_run=run, status="partial")

    advanced = advance_list_cursors(
        session,
        x_list=x_list,
        sync_run=run,
        latest_observed_post_id="new-head",
        latest_seen_at=stored_at - timedelta(minutes=1),
        latest_seen_post_id="valid-8",
    )

    assert advanced == (True, False)
    assert x_list.latest_observed_post_id == "new-head"
    assert x_list.latest_seen_at == stored_at
    assert x_list.latest_seen_post_id == "valid-9"
