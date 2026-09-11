from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from x_digest.models import Base
from x_digest.repositories.jobs import advance_list_watermark, begin_sync_run, finish_sync_run
from x_digest.repositories.lists import upsert_list


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session


def test_failed_sync_does_not_advance_list_watermark(session: Session) -> None:
    x_list = upsert_list(session, platform_list_id="list-a", name="First")
    run = begin_sync_run(session, list_id=x_list.id)
    finish_sync_run(session, sync_run=run, status="failed")

    advanced = advance_list_watermark(
        session,
        x_list=x_list,
        sync_run=run,
        latest_seen_at=datetime.now(UTC),
        latest_seen_post_id="42",
    )

    assert advanced is False
    assert x_list.latest_seen_at is None
