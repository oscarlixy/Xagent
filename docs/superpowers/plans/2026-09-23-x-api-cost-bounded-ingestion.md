# Cost-Bounded X List Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound every X List synchronization to 50 returned posts while using a one-post unchanged probe, a 24-hour recovery window, and cursor semantics that prevent partial records from causing repeated historical reads.

**Architecture:** Preserve the existing List Posts source and page-at-a-time durable writes. Add raw response accounting and an observed-head cursor, then let `IngestionService` run a newest-first state machine with a frozen cutoff, small pages, explicit stop reasons, and an atomic terminal cursor update. Keep health (`succeeded | partial | failed`) independent from completeness (`truncated`) and surface both through existing pipeline and status APIs.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, SQLAlchemy 2, Alembic, httpx, pytest, Ruff, MyPy, PostgreSQL/SQLite test coverage, Next.js TypeScript frontend.

**Spec:** `docs/superpowers/specs/2026-09-23-x-api-cost-bounded-ingestion-design.md`

## Global Constraints

- Keep `GET /2/lists/{id}/tweets`; do not introduce Recent Search, X Activity subscriptions, queues, or new X permissions.
- Defaults are exactly: 24-hour backfill, 10-minute overlap, page size 10, 50 returned posts, and six requests per run.
- Every provider `data` item counts toward the post budget even when local parsing rejects it; provider `errors` entries do not count as returned posts.
- A safely completed `partial` or truncated run advances eligible cursors; a `failed` run advances neither cursor.
- Posts older than the effective cutoff are not persisted, and a page containing an older valid post stops further pagination.
- Provider calls remain outside active database transactions; page writes and terminal metadata use short transactions.
- API changes are additive and preserve the public `succeeded | partial | failed` status enum.
- Do not put raw provider bodies, secrets, or unsafe upstream text in persistence, logs, or API responses.
- No automated test may call the paid X API.

## Review Focus

- Empty or rejected-only pages with a `next_token` must remain bounded by the six-request safety limit and report budget truncation instead of looping indefinitely; Task 4 pins this behavior.
- A configured post budget smaller than the page size must reduce `max_results` to the exact remaining budget; Task 4 pins this behavior.
- A repeated pagination token must not create an unbounded loop; Task 4 proves the request safety limit terminates it as truncated.
- Naive stored timestamps and timezone-aware clocks must produce one UTC cutoff without comparison errors; Task 4 pins normalization.
- Failure of the final completion commit after page rows were persisted must leave both cursors unchanged and the run failed; Task 5 pins atomicity.

---

## File Structure

- `backend/alembic/versions/0006_cost_bounded_ingestion.py`: additive schema migration and reversible downgrade.
- `backend/src/x_digest/models/source.py`: observed-head cursor on `XList`.
- `backend/src/x_digest/models/jobs.py`: per-run cost/completeness fields and `SyncRejection` rows.
- `backend/src/x_digest/models/__init__.py`: export `SyncRejection`.
- `backend/src/x_digest/sources/types.py`: raw response accounting carried by `SourcePage`.
- `backend/src/x_digest/sources/x_api.py`: populate raw count and head ID before parsing.
- `backend/src/x_digest/repositories/jobs.py`: terminal cursor advancement rules and rejection persistence helpers.
- `backend/src/x_digest/services/ingestion.py`: probe, cutoff, budget, pagination, stop reason, and terminal decision state machine.
- `backend/src/x_digest/config.py`: new defaults and validation.
- `backend/src/x_digest/main.py`, `backend/src/x_digest/scheduler.py`: pass settings into both production pipeline builders.
- `backend/src/x_digest/services/pipeline.py`: propagate cost/completeness metadata to API results, durable debug metadata, and logs.
- `backend/src/x_digest/observability.py`, `backend/src/x_digest/api/schemas.py`: safe additive status exposure.
- `backend/src/x_digest/api/lists.py`: preserve rejection referential integrity when deleting a List.
- `README.md`: document the new bounded synchronization behavior and configuration.
- Existing focused test modules own regression coverage; no new broad integration fixture is needed.

### Task 1: Persist Cursor, Cost, Completeness, and Rejections

**Files:**
- Create: `backend/alembic/versions/0006_cost_bounded_ingestion.py`
- Modify: `backend/src/x_digest/models/source.py`
- Modify: `backend/src/x_digest/models/jobs.py`
- Modify: `backend/src/x_digest/models/__init__.py`
- Modify: `backend/src/x_digest/api/lists.py`
- Test: `backend/tests/test_migrations.py`
- Test: `backend/tests/test_schema.py`
- Test: `backend/tests/api/test_api.py`

**Interfaces:**
- Consumes: existing `XList`, `SyncRun`, `Base`, `new_id`, and List deletion transaction.
- Produces: `XList.latest_observed_post_id: str | None`; `SyncRun.posts_returned: int`, `truncated: bool`, and `stop_reason: str | None`; `SyncRejection(sync_run_id, item_identifier, reason)`.

- [ ] **Step 1: Add failing schema and migration tests**

Extend `backend/tests/test_migrations.py` to inspect the migrated columns and table:

```python
inspector = inspect(create_engine(f"sqlite+pysqlite:///{database_path}"))
assert "sync_rejections" in inspector.get_table_names()
assert {column["name"] for column in inspector.get_columns("x_lists")} >= {
    "latest_observed_post_id"
}
assert {column["name"] for column in inspector.get_columns("sync_runs")} >= {
    "posts_returned",
    "truncated",
    "stop_reason",
}
```

Add a model test in `backend/tests/test_schema.py`:

```python
def test_sync_rejection_belongs_to_a_run(session: Session) -> None:
    x_list = XList(platform_list_id="list-a", name="AI")
    session.add(x_list)
    session.flush()
    run = SyncRun(list_id=x_list.id, status="partial")
    rejection = SyncRejection(
        sync_run=run,
        item_identifier="post-42",
        reason="invalid_item",
    )
    session.add_all([run, rejection])
    session.commit()

    assert rejection.sync_run_id == run.id
    assert run.rejections == [rejection]
```

Extend the existing List deletion API test to create a rejection for one of its `SyncRun` rows and assert deletion removes both rows without a foreign-key error.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
cd backend
uv run pytest tests/test_migrations.py tests/test_schema.py tests/api/test_api.py -q
```

Expected: failures because the columns, table, model, and relationship do not exist.

- [ ] **Step 3: Add the model fields and rejection relationship**

Implement the model contract:

```python
# models/source.py, XList
latest_observed_post_id: Mapped[str | None] = mapped_column(String(32))

# models/jobs.py, SyncRun
posts_returned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
stop_reason: Mapped[str | None] = mapped_column(String(64))
rejections: Mapped[list[SyncRejection]] = relationship(
    back_populates="sync_run", cascade="all, delete-orphan", passive_deletes=True
)

class SyncRejection(Base):
    __tablename__ = "sync_rejections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    sync_run_id: Mapped[str] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False
    )
    item_identifier: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)
    sync_run: Mapped[SyncRun] = relationship(back_populates="rejections")
```

Import `Boolean`, export `SyncRejection` from `models/__init__.py`, and use postponed annotations or quoted relationship targets so MyPy and SQLAlchemy can resolve both classes.

- [ ] **Step 4: Add migration `0006_cost_bounded_ingestion`**

Create an additive migration with `down_revision = "0005_thread_identity"`:

```python
def upgrade() -> None:
    op.add_column("x_lists", sa.Column("latest_observed_post_id", sa.String(length=32)))
    op.add_column(
        "sync_runs",
        sa.Column("posts_returned", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "sync_runs",
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("sync_runs", sa.Column("stop_reason", sa.String(length=64)))
    op.create_table(
        "sync_rejections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "sync_run_id",
            sa.String(length=36),
            sa.ForeignKey("sync_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_identifier", sa.String(length=64)),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sync_rejections")
    op.drop_column("sync_runs", "stop_reason")
    op.drop_column("sync_runs", "truncated")
    op.drop_column("sync_runs", "posts_returned")
    op.drop_column("x_lists", "latest_observed_post_id")
```

Keep the server defaults so existing rows migrate safely. Do not remove them in a follow-up operation; they match the desired zero/false defaults.

- [ ] **Step 5: Make bulk List deletion explicitly remove rejection rows**

Even though the new foreign key cascades in PostgreSQL, keep deletion deterministic on SQLite configurations where foreign-key enforcement may differ:

```python
run_ids = select(SyncRun.id).where(SyncRun.list_id == list_id)
self._session.execute(
    delete(SyncRejection).where(SyncRejection.sync_run_id.in_(run_ids))
)
self._session.execute(delete(SyncRun).where(SyncRun.list_id == list_id))
```

Import `SyncRejection` in `api/lists.py` and leave membership deletion unchanged.

- [ ] **Step 6: Run focused tests and static checks**

Run:

```bash
cd backend
uv run pytest tests/test_migrations.py tests/test_schema.py tests/api/test_api.py -q
uv run ruff check src/x_digest/models src/x_digest/api/lists.py alembic/versions/0006_cost_bounded_ingestion.py tests/test_migrations.py tests/test_schema.py
uv run mypy src
```

Expected: all commands exit zero.

- [ ] **Step 7: Commit the persistence contract**

```bash
git add backend/alembic/versions/0006_cost_bounded_ingestion.py backend/src/x_digest/models backend/src/x_digest/api/lists.py backend/tests/test_migrations.py backend/tests/test_schema.py backend/tests/api/test_api.py
git commit -m "feat: persist bounded ingestion state"
```

### Task 2: Preserve Raw X Page Accounting

**Files:**
- Modify: `backend/src/x_digest/sources/types.py`
- Modify: `backend/src/x_digest/sources/x_api.py`
- Test: `backend/tests/sources/test_x_api.py`

**Interfaces:**
- Consumes: X response `data`, `errors`, `meta.next_token`, and existing parsed `RawPost`/`RejectedItem` values.
- Produces: `SourcePage.returned_count: int | None` and `SourcePage.head_post_id: str | None`; real X pages always provide an integer count.

- [ ] **Step 1: Write failing source metadata tests**

Extend the successful response test:

```python
assert page.returned_count == 1
assert page.head_post_id == "tweet-1"
```

Add a malformed-head test whose first raw data item has a string ID but cannot parse because its author expansion is missing:

```python
def test_fetch_page_preserves_raw_count_and_head_before_item_rejection() -> None:
    payload = {
        "data": [
            {"id": "bad-head", "text": "missing fields"},
            _response_payload()["data"][0],
        ],
        "includes": _response_payload()["includes"],
        "errors": [{"value": "provider-error"}],
        "meta": {},
    }
    source, _tokens, client = _source(lambda _request: httpx.Response(200, json=payload))
    try:
        page = asyncio.run(
            source.fetch_page(list_id=LIST_ID, pagination_token=None, max_results=10)
        )
    finally:
        asyncio.run(client.aclose())

    assert page.returned_count == 2
    assert page.head_post_id == "bad-head"
    assert len(page.posts) == 1
    assert len(page.rejected_items) == 2
```

- [ ] **Step 2: Run the source tests and verify failure**

Run:

```bash
cd backend
uv run pytest tests/sources/test_x_api.py -q
```

Expected: assertions fail because `SourcePage` lacks raw accounting fields.

- [ ] **Step 3: Extend `SourcePage` without breaking simple fake fixtures**

Use optional accounting for hand-built pages; ingestion will infer a conservative count for legacy fakes only:

```python
@dataclass(frozen=True)
class SourcePage:
    posts: tuple[RawPost, ...] = ()
    rejected_items: tuple[RejectedItem, ...] = ()
    next_token: str | None = None
    request_id: str | None = None
    returned_count: int | None = None
    head_post_id: str | None = None
```

Do not infer the provider head in this data class: only the adapter sees raw ordering before parsing.

- [ ] **Step 4: Populate metadata before parsing**

In `_parse_page`, compute metadata immediately after validating `data`:

```python
returned_count = len(data)
first_item = data[0] if data else None
raw_head = first_item.get("id") if isinstance(first_item, Mapping) else None
head_post_id = raw_head if isinstance(raw_head, str) else None
```

Return both fields on `SourcePage`. Do not count `errors` toward `returned_count`, and do not expose error details.

- [ ] **Step 5: Run source tests and static checks**

Run:

```bash
cd backend
uv run pytest tests/sources/test_x_api.py -q
uv run ruff check src/x_digest/sources tests/sources/test_x_api.py
uv run mypy src
```

Expected: all commands exit zero.

- [ ] **Step 6: Commit the adapter contract**

```bash
git add backend/src/x_digest/sources/types.py backend/src/x_digest/sources/x_api.py backend/tests/sources/test_x_api.py
git commit -m "feat: expose raw X page accounting"
```

### Task 3: Define Safe Terminal Cursor Semantics

**Files:**
- Modify: `backend/src/x_digest/repositories/jobs.py`
- Test: `backend/tests/repositories/test_jobs.py`

**Interfaces:**
- Consumes: terminal `SyncRun.status`, an `XList`, an optional observed head, and an optional newest valid `(datetime, post_id)` pair.
- Produces: `advance_list_cursors(...) -> tuple[bool, bool]`, returning `(observed_head_advanced, valid_watermark_advanced)`.

- [ ] **Step 1: Add failing repository tests for each terminal state**

Replace the single-watermark-only assumptions with explicit cursor tests:

```python
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
```

- [ ] **Step 2: Run repository tests and verify failure**

Run:

```bash
cd backend
uv run pytest tests/repositories/test_jobs.py -q
```

Expected: import or assertion failures because `advance_list_cursors` does not exist and partial runs cannot advance.

- [ ] **Step 3: Replace the old gate with the two-cursor helper**

Implement this exact public signature:

```python
def advance_list_cursors(
    session: Session,
    *,
    x_list: XList,
    sync_run: SyncRun,
    latest_observed_post_id: str | None,
    latest_seen_at: datetime | None,
    latest_seen_post_id: str | None,
) -> tuple[bool, bool]:
```

The implementation must:

```python
if sync_run.status not in {"succeeded", "partial"}:
    return False, False

observed_advanced = latest_observed_post_id is not None
if observed_advanced:
    x_list.latest_observed_post_id = latest_observed_post_id

watermark_advanced = False
if latest_seen_at is not None and latest_seen_post_id is not None:
    candidate = (_as_utc(latest_seen_at), latest_seen_post_id)
    stored = (
        (_as_utc(x_list.latest_seen_at), x_list.latest_seen_post_id or "")
        if x_list.latest_seen_at is not None
        else None
    )
    if stored is None or candidate > stored:
        x_list.latest_seen_at = latest_seen_at
        x_list.latest_seen_post_id = latest_seen_post_id
        watermark_advanced = True
session.flush()
return observed_advanced, watermark_advanced
```

Remove `advance_list_watermark` and update its direct test imports now; Task 4 will update the service caller.

- [ ] **Step 4: Run repository tests and static checks**

Run:

```bash
cd backend
uv run pytest tests/repositories/test_jobs.py -q
uv run ruff check src/x_digest/repositories/jobs.py tests/repositories/test_jobs.py
uv run mypy src
```

Expected: all commands exit zero.

- [ ] **Step 5: Commit cursor semantics**

```bash
git add backend/src/x_digest/repositories/jobs.py backend/tests/repositories/test_jobs.py
git commit -m "feat: advance safe ingestion cursors"
```

### Task 4: Implement Probe, Time Boundary, and Bounded Pagination

**Files:**
- Modify: `backend/src/x_digest/services/ingestion.py`
- Test: `backend/tests/services/test_ingestion.py`

**Interfaces:**
- Consumes: `SourcePage.returned_count`, `head_post_id`, `next_token`; `advance_list_cursors`; `page_size`, `max_posts`, `max_pages`, `overlap`, `backfill_window`, and `clock` constructor inputs.
- Produces: `SyncResult(..., posts_returned: int, truncated: bool, stop_reason: StopReason)` where `StopReason = Literal["unchanged", "watermark_reached", "time_window_reached", "provider_exhausted", "budget_exhausted"]`.

- [ ] **Step 1: Add a deterministic page builder and frozen clock to ingestion tests**

Add helpers that make the resource count explicit:

```python
NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


def _page(
    *posts: RawPost,
    next_token: str | None = None,
    rejected_items: tuple[RejectedItem, ...] = (),
    returned_count: int | None = None,
    head_post_id: str | None = None,
) -> SourcePage:
    return SourcePage(
        posts=posts,
        rejected_items=rejected_items,
        next_token=next_token,
        returned_count=(len(posts) + len(rejected_items) if returned_count is None else returned_count),
        head_post_id=head_post_id or (posts[0].id if posts else None),
    )


def _new_list(
    *,
    observed_head: str | None = None,
    latest_seen_at: datetime | None = None,
) -> tuple[sessionmaker[Session], str]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        x_list.latest_observed_post_id = observed_head
        x_list.latest_seen_at = latest_seen_at
        x_list.latest_seen_post_id = "stored-watermark" if latest_seen_at else None
        list_id = x_list.id
    return factory, list_id


def _service(
    factory: sessionmaker[Session],
    source: FakeXSource,
    *,
    max_pages: int = 6,
    max_posts: int = 50,
    page_size: int = 10,
) -> IngestionService:
    return IngestionService(
        session_factory=factory,
        source=source,
        max_pages=max_pages,
        max_posts=max_posts,
        page_size=page_size,
        overlap=timedelta(minutes=10),
        backfill_window=timedelta(hours=24),
        clock=lambda: NOW,
    )


def _stored_platform_ids(factory: sessionmaker[Session]) -> set[str]:
    with factory() as session:
        return {post.platform_post_id for post in session.scalars(select(Post))}
```

Import `Session` for the helper annotations. The helper fixes `clock=lambda: NOW` for every new boundary-sensitive test.

- [ ] **Step 2: Write failing tests for probe and initialization behavior**

Add:

```python
def test_initialized_unchanged_list_stops_after_one_result_probe() -> None:
    factory, list_id = _new_list(
        observed_head="head", latest_seen_at=NOW - timedelta(hours=1)
    )
    source = FakeXSource({None: _page(_raw_post("head", NOW), head_post_id="head")})
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    assert [call[2] for call in source.calls] == [1]
    assert result.posts_returned == 1
    assert result.stop_reason == "unchanged"
    assert result.truncated is False


def test_uninitialized_list_starts_with_ten_results_and_skips_old_posts() -> None:
    factory, list_id = _new_list()
    source = FakeXSource(
        {
            None: _page(
                _raw_post("new", NOW - timedelta(hours=1)),
                _raw_post("old", NOW - timedelta(hours=25)),
                head_post_id="new",
            )
        }
    )
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    assert [call[2] for call in source.calls] == [10]
    assert result.stop_reason == "time_window_reached"
    assert _stored_platform_ids(factory) == {"new"}
```

- [ ] **Step 3: Write failing tests for page size, budget, and stop boundaries**

Add complete fixtures for these cases:

```python
def test_incremental_thirty_posts_returns_at_most_forty_one_items() -> None:
    factory, list_id = _new_list(
        observed_head="old-head", latest_seen_at=NOW - timedelta(hours=2)
    )
    new_posts = tuple(
        _raw_post(f"new-{index:02d}", NOW - timedelta(minutes=index))
        for index in range(30)
    )
    old = _raw_post("old", NOW - timedelta(hours=3))
    source = FakeXSource(
        {
            None: _page(new_posts[0], next_token="p1", returned_count=1),
            "p1": _page(*new_posts[1:11], next_token="p2", returned_count=10),
            "p2": _page(*new_posts[11:21], next_token="p3", returned_count=10),
            "p3": _page(*new_posts[21:30], old, next_token="p4", returned_count=10),
        }
    )
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    assert [call[2] for call in source.calls] == [1, 10, 10, 10]
    assert result.posts_returned == 31
    assert result.posts_returned <= 41
    assert result.truncated is False
    assert result.stop_reason == "watermark_reached"


def test_remaining_budget_reduces_requested_page_size() -> None:
    factory, list_id = _new_list()
    source = FakeXSource(
        {None: _page(_raw_post("head", NOW), next_token="p2", returned_count=7)}
    )
    service = _service(factory, source, max_posts=7, page_size=10)
    asyncio.run(service.sync_list(list_id))

    assert [call[2] for call in source.calls] == [7]


def test_mixed_page_crossing_recent_watermark_does_not_fetch_next_page() -> None:
    factory, list_id = _new_list(latest_seen_at=NOW - timedelta(minutes=30))
    source = FakeXSource(
        {
            None: _page(
                _raw_post("new", NOW),
                _raw_post("old", NOW - timedelta(minutes=41)),
                next_token="p2",
            ),
            "p2": _page(_raw_post("must-not-fetch", NOW - timedelta(hours=1))),
        }
    )
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    assert [call[1] for call in source.calls] == [None]
    assert result.stop_reason == "watermark_reached"


def test_naive_watermark_is_normalized_against_aware_clock() -> None:
    factory, list_id = _new_list(latest_seen_at=(NOW - timedelta(minutes=30)).replace(tzinfo=None))
    source = FakeXSource(
        {None: _page(_raw_post("old", NOW - timedelta(minutes=41)))}
    )
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    assert result.stop_reason == "watermark_reached"


def test_repeated_pagination_token_stops_at_page_limit_as_truncated() -> None:
    factory, list_id = _new_list()
    source = FakeXSource(
        {
            None: _page(_raw_post("head", NOW), next_token="repeat"),
            "repeat": _page(_raw_post("again", NOW), next_token="repeat"),
        }
    )
    result = asyncio.run(_service(factory, source, max_pages=3).sync_list(list_id))

    assert result.pages_fetched == 3
    assert result.truncated is True
    assert result.stop_reason == "budget_exhausted"


def test_rejected_only_pages_with_next_tokens_are_bounded() -> None:
    factory, list_id = _new_list()
    rejected = (RejectedItem(identifier="bad", reason="invalid_item"),)
    source = FakeXSource(
        {
            None: _page(rejected_items=rejected, next_token="p2", returned_count=1),
            "p2": _page(rejected_items=rejected, next_token="p3", returned_count=1),
            "p3": _page(rejected_items=rejected, next_token="p4", returned_count=1),
        }
    )
    result = asyncio.run(_service(factory, source, max_pages=3).sync_list(list_id))

    assert result.pages_fetched == 3
    assert result.truncated is True


def test_empty_page_with_next_token_stops_as_provider_exhausted() -> None:
    factory, list_id = _new_list()
    source = FakeXSource({None: _page(next_token="p2", returned_count=0)})
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    assert len(source.calls) == 1
    assert result.stop_reason == "provider_exhausted"
    assert result.truncated is False
```

- [ ] **Step 4: Run the focused tests and verify failure**

Run:

```bash
cd backend
uv run pytest tests/services/test_ingestion.py -q
```

Expected: new tests fail because the service lacks probe, clock, page-size, backfill, accounting, and stop-reason behavior.

- [ ] **Step 5: Extend result and mutable state types**

Add:

```python
StopReason = Literal[
    "unchanged",
    "watermark_reached",
    "time_window_reached",
    "provider_exhausted",
    "budget_exhausted",
]
Clock = Callable[[], datetime]

@dataclass(frozen=True)
class SyncResult:
    run_id: str
    status: Literal["succeeded", "partial", "failed"]
    pages_fetched: int
    posts_seen: int
    posts_created: int
    duplicates: int
    rejected: int
    posts_returned: int
    truncated: bool
    stop_reason: StopReason
```

Mirror these values in `_MutableSyncResult`, with `stop_reason: StopReason | None` until termination is selected. Track `latest_observed_post_id` separately from the newest valid post.

- [ ] **Step 6: Extend constructor validation and freeze the cutoff once**

Use this constructor shape:

```python
def __init__(
    self,
    *,
    session_factory: sessionmaker[Session],
    source: XSource,
    max_pages: int = 6,
    max_posts: int = 50,
    page_size: int = 10,
    overlap: timedelta = timedelta(minutes=10),
    backfill_window: timedelta = timedelta(hours=24),
    clock: Clock | None = None,
) -> None:
```

Require positive page/post budgets, `1 <= page_size <= 100`, non-negative overlap, and positive backfill window. Set `run_now = _as_utc(self._clock())` once. Compute both candidate cutoffs, choose their maximum as `effective_cutoff`, and retain `cutoff_reason` as `watermark_reached` when the watermark cutoff wins or `time_window_reached` when the recovery cutoff wins.

- [ ] **Step 7: Implement request sizing and raw-count accounting**

Before each fetch:

```python
initialized = latest_observed_post_id is not None
first_request_size = 1 if initialized else self._page_size
request_size = min(
    first_request_size if result.pages_fetched == 0 else self._page_size,
    self._max_posts - result.posts_returned,
)
```

After each response:

```python
returned_count = (
    page.returned_count
    if page.returned_count is not None
    else len(page.posts) + len(page.rejected_items)
)
if returned_count < 0:
    raise ValueError("Source returned a negative item count")
page_result.posts_returned = returned_count
```

The source request remains outside the following page transaction. Persist only valid posts at or after `effective_cutoff`; valid older posts prove the boundary but are not stored.

- [ ] **Step 8: Implement termination precedence**

After a page commits, select exactly one reason:

```python
if first_page and initialized and page.head_post_id == stored_head:
    stop_reason = "unchanged"
elif crossed_effective_cutoff:
    stop_reason = cutoff_reason
elif page.next_token is None or returned_count == 0:
    stop_reason = "provider_exhausted"
elif result.posts_returned >= self._max_posts or result.pages_fetched >= self._max_pages:
    stop_reason = "budget_exhausted"
    result.truncated = True
else:
    pagination_token = page.next_token
    continue
```

An empty page stops as provider exhaustion even if it carries a token. A non-empty token cycle remains bounded by `max_pages`. Capture the first page's `head_post_id` but defer cursor persistence to completion.

- [ ] **Step 9: Complete successful runs through `advance_list_cursors`**

In `_complete_run`, require a non-null stop reason, write `posts_returned`, `truncated`, and `stop_reason` to `SyncRun`, finish it as `succeeded` or `partial`, and call:

```python
advance_list_cursors(
    session,
    x_list=x_list,
    sync_run=durable_run,
    latest_observed_post_id=result.latest_observed_post_id,
    latest_seen_at=result.latest_seen_at,
    latest_seen_post_id=result.latest_seen_post_id,
)
```

Update `_record_counts` and `SyncResult` construction with the new values.

- [ ] **Step 10: Run ingestion and repository tests**

Run:

```bash
cd backend
uv run pytest tests/services/test_ingestion.py tests/repositories/test_jobs.py -q
uv run ruff check src/x_digest/services/ingestion.py tests/services/test_ingestion.py
uv run mypy src
```

Expected: all commands exit zero.

- [ ] **Step 11: Commit the bounded state machine**

```bash
git add backend/src/x_digest/services/ingestion.py backend/tests/services/test_ingestion.py
git commit -m "feat: bound X List pagination cost"
```

### Task 5: Persist Partial Rejections and Prove Failure Atomicity

**Files:**
- Modify: `backend/src/x_digest/services/ingestion.py`
- Test: `backend/tests/services/test_ingestion.py`
- Test: `backend/tests/services/test_pipeline.py`

**Interfaces:**
- Consumes: `SourcePage.rejected_items`, terminal ingestion state, `SyncRejection`, and Task 4 cursor completion.
- Produces: one safe `SyncRejection` per rejected source item; partial and truncated results that still advance eligible cursors; failures that preserve page rows but never cursors.

- [ ] **Step 1: Replace the old partial-watermark regression with the accepted behavior**

Rewrite the existing rejected-page test:

```python
def test_rejected_page_is_partial_records_rejection_and_advances_cursors() -> None:
    factory, list_id = _new_list()
    source = FakeXSource(
        {
            None: _page(
                _raw_post("101", NOW),
                rejected_items=(
                    RejectedItem(identifier="102", reason="provider_rejected"),
                ),
                returned_count=2,
                head_post_id="101",
            )
        }
    )
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    with factory() as session:
        run = session.scalar(select(SyncRun))
        rejection = session.scalar(select(SyncRejection))
        x_list = session.get(XList, list_id)
        assert run is not None
        assert rejection is not None
        assert x_list is not None
        assert result.status == "partial"
        assert result.rejected == 1
        assert (rejection.item_identifier, rejection.reason) == (
            "102",
            "provider_rejected",
        )
        assert x_list.latest_observed_post_id == "101"
        assert x_list.latest_seen_post_id == "101"
```

Update the pipeline test formerly named `test_provider_rejections_remain_partial_through_pipeline_and_withhold_watermark` so it expects the watermark and observed head to advance while the pipeline remains partial.

Update `make_pipeline` so its fake page models raw source metadata:

```python
SourcePage(
    posts=posts,
    rejected_items=rejected_items,
    returned_count=len(posts) + len(rejected_items),
    head_post_id=posts[0].id if posts else None,
)
```

- [ ] **Step 2: Add truncation replay and terminal-failure tests**

Add:

```python
def test_budget_truncation_advances_head_and_next_run_uses_unchanged_probe() -> None:
    factory, list_id = _new_list()
    pages: dict[str | None, SourcePage] = {}
    for page_index in range(5):
        token = None if page_index == 0 else f"p{page_index}"
        next_token = f"p{page_index + 1}"
        posts = tuple(
            _raw_post(
                f"post-{page_index * 10 + offset:02d}",
                NOW - timedelta(minutes=page_index * 10 + offset),
            )
            for offset in range(10)
        )
        pages[token] = _page(*posts, next_token=next_token, returned_count=10)
    source = FakeXSource(pages)
    first = asyncio.run(_service(factory, source).sync_list(list_id))

    source.pages = {
        None: _page(_raw_post("post-00", NOW), returned_count=1, head_post_id="post-00")
    }
    second = asyncio.run(_service(factory, source).sync_list(list_id))

    assert first.truncated is True
    assert first.stop_reason == "budget_exhausted"
    assert second.stop_reason == "unchanged"
    assert second.posts_returned == 1


assert run.status == "failed"
assert x_list.latest_observed_post_id is None
assert x_list.latest_seen_at is None
assert x_list.latest_seen_post_id is None
```

Place these assertions in the existing `test_final_completion_commit_failure_marks_the_durable_run_failed` after it reloads `run` and `x_list`. Add the same cursor assertions to the existing cancellation and second-page failure tests.

Also assert cancellation and second-page upstream failure retain all stored cursor values even when the first page's posts committed.

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
cd backend
uv run pytest tests/services/test_ingestion.py tests/services/test_pipeline.py -q
```

Expected: rejection-row and new cursor assertions fail until persistence is implemented and old withholding expectations are removed.

- [ ] **Step 4: Persist safe rejection rows in each page transaction**

After loading the durable run inside the page transaction, add:

```python
for rejected in page.rejected_items:
    session.add(
        SyncRejection(
            sync_run_id=run_id,
            item_identifier=rejected.identifier,
            reason=rejected.reason,
        )
    )
```

Keep only the already-normalized `identifier` and safe reason. Never persist response bodies or exception strings.

- [ ] **Step 5: Preserve new terminal metadata during failure handling**

Ensure `_record_counts` always writes `posts_returned`, while `_finish_failed_run` explicitly leaves `truncated=False` and `stop_reason=None` because the traversal did not safely complete. It may retain page/post/rejection counts for diagnosis.

The final completion transaction must update status, counts, completeness, stop reason, and both cursors together. Existing best-effort failure handling runs only after that transaction rolls back.

- [ ] **Step 6: Run focused tests and static checks**

Run:

```bash
cd backend
uv run pytest tests/services/test_ingestion.py tests/services/test_pipeline.py -q
uv run ruff check src/x_digest/services/ingestion.py tests/services/test_ingestion.py tests/services/test_pipeline.py
uv run mypy src
```

Expected: all commands exit zero.

- [ ] **Step 7: Commit partial and failure semantics**

```bash
git add backend/src/x_digest/services/ingestion.py backend/tests/services/test_ingestion.py backend/tests/services/test_pipeline.py
git commit -m "fix: advance cursors after partial ingestion"
```

### Task 6: Wire Conservative Defaults into API and Scheduler Pipelines

**Files:**
- Modify: `backend/src/x_digest/config.py`
- Modify: `backend/src/x_digest/main.py`
- Modify: `backend/src/x_digest/scheduler.py`
- Test: `backend/tests/test_health.py`
- Test: `backend/tests/test_scheduler.py`

**Interfaces:**
- Consumes: Pydantic environment loading and Task 4 `IngestionService` constructor.
- Produces: `Settings.x_page_size=10`, `x_max_posts_per_sync=50`, `x_max_pages_per_sync=6`, `x_ingestion_backfill_hours=24`, with identical wiring in web and scheduler processes.

- [ ] **Step 1: Add failing settings-default and wiring tests**

Add a direct default test:

```python
def test_ingestion_cost_defaults_are_conservative() -> None:
    settings = Settings(_env_file=None)
    assert settings.x_page_size == 10
    assert settings.x_max_posts_per_sync == 50
    assert settings.x_max_pages_per_sync == 6
    assert settings.x_ingestion_backfill_hours == 24
    assert settings.x_ingestion_overlap_minutes == 10
```

In `test_health.py`, monkeypatch `x_digest.main.IngestionService` with a capturing constructor before calling `create_app`:

```python
captured: dict[str, object] = {}


def capture_ingestion(**kwargs: object) -> object:
    captured.update(kwargs)
    return object()


monkeypatch.setattr(main_module, "IngestionService", capture_ingestion)
main_module.create_app(
    settings=Settings(_env_file=None),
    session_factory=session_factory,
    _validate_token_on_create=False,
)

assert {
    key: captured[key]
    for key in ("page_size", "max_posts", "max_pages", "overlap", "backfill_window")
} == {
    "page_size": 10,
    "max_posts": 50,
    "max_pages": 6,
    "overlap": timedelta(minutes=10),
    "backfill_window": timedelta(hours=24),
}
```

In `test_scheduler.py`, repeat the capture with the scheduler module and close the created clients:

```python
captured: dict[str, object] = {}


def capture_ingestion(**kwargs: object) -> object:
    captured.update(kwargs)
    return object()


monkeypatch.setattr(scheduler_module, "IngestionService", capture_ingestion)
_pipeline, oauth_client, source = scheduler_module.build_production_pipeline(
    Settings(_env_file=None), session_factory
)
asyncio.run(source.aclose())
asyncio.run(oauth_client.aclose())

assert {
    key: captured[key]
    for key in ("page_size", "max_posts", "max_pages", "overlap", "backfill_window")
} == {
    "page_size": 10,
    "max_posts": 50,
    "max_pages": 6,
    "overlap": timedelta(minutes=10),
    "backfill_window": timedelta(hours=24),
}
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```bash
cd backend
uv run pytest tests/test_health.py tests/test_scheduler.py -q
```

Expected: missing fields and old defaults cause failures.

- [ ] **Step 3: Add validated settings**

Change and add:

```python
x_max_pages_per_sync: int = Field(default=6, ge=1, le=100)
x_max_posts_per_sync: int = Field(default=50, ge=1, le=10_000)
x_page_size: int = Field(default=10, ge=1, le=100)
x_ingestion_backfill_hours: int = Field(default=24, ge=1, le=24 * 30)
x_ingestion_overlap_minutes: int = Field(default=10, ge=0, le=1440)
```

- [ ] **Step 4: Wire both service construction paths**

In `main.py` and `scheduler.py`, pass:

```python
max_pages=settings.x_max_pages_per_sync,
max_posts=settings.x_max_posts_per_sync,
page_size=settings.x_page_size,
overlap=timedelta(minutes=settings.x_ingestion_overlap_minutes),
backfill_window=timedelta(hours=settings.x_ingestion_backfill_hours),
```

Do not change scheduling cadence or enabled-List selection.

- [ ] **Step 5: Run focused tests and static checks**

Run:

```bash
cd backend
uv run pytest tests/test_health.py tests/test_scheduler.py -q
uv run ruff check src/x_digest/config.py src/x_digest/main.py src/x_digest/scheduler.py tests/test_health.py tests/test_scheduler.py
uv run mypy src
```

Expected: all commands exit zero.

- [ ] **Step 6: Commit configuration wiring**

```bash
git add backend/src/x_digest/config.py backend/src/x_digest/main.py backend/src/x_digest/scheduler.py backend/tests/test_health.py backend/tests/test_scheduler.py
git commit -m "chore: configure bounded X ingestion"
```

### Task 7: Expose Cost and Completeness Safely, Document, and Verify

**Files:**
- Modify: `backend/src/x_digest/services/pipeline.py`
- Modify: `backend/src/x_digest/observability.py`
- Modify: `backend/src/x_digest/api/schemas.py`
- Modify: `backend/tests/services/test_pipeline.py`
- Modify: `backend/tests/test_observability.py`
- Modify: `backend/tests/api/test_api.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 4 `SyncResult.posts_returned`, `truncated`, and `stop_reason`; existing `StageResult`, `PipelineResult`, durable debug metadata, and status sanitization.
- Produces: additive `StageResult.truncated: bool` and `stop_reason: str | None`; safe API/status/log fields and operator documentation.

- [ ] **Step 1: Write failing pipeline and API tests**

Extend the ingestion stage assertions:

```python
assert result.stages["ingestion"].counts["posts_returned"] == 2
assert result.stages["ingestion"].truncated is False
assert result.stages["ingestion"].stop_reason == "provider_exhausted"
```

Add an API fake returning a truncated ingestion stage and assert JSON remains status-compatible:

```python
assert response.json()["status"] == "succeeded"
assert response.json()["stages"]["ingestion"]["truncated"] is True
assert response.json()["stages"]["ingestion"]["stop_reason"] == "budget_exhausted"
```

Add observability assertions that `latest_run` exposes the persisted top-level `posts_returned`, `truncated`, and a known stop reason, while an unknown stop reason and nested provider payload remain omitted or normalized to `None`.

- [ ] **Step 2: Run focused backend tests and verify failure**

Run:

```bash
cd backend
uv run pytest tests/services/test_pipeline.py tests/test_observability.py tests/api/test_api.py -q
```

Expected: schema/result assertions fail before exposure is implemented.

- [ ] **Step 3: Extend internal and API stage contracts**

Add defaults so non-ingestion stages require no call-site edits:

```python
@dataclass(frozen=True)
class StageResult:
    counts: dict[str, int]
    duration_ms: int
    last_error: str | None = None
    truncated: bool = False
    stop_reason: str | None = None
```

Mirror them in `StageResultResponse`, restricting `stop_reason` to the five safe literals. Construct the ingestion stage with `posts_returned`, `sync.truncated`, and `sync.stop_reason`. Persist both fields inside each stage's `debug_metadata` and emit them in structured completion logs.

- [ ] **Step 4: Sanitize status exposure from typed database columns**

Add `posts_returned` to `_SAFE_STAGE_COUNTS` and expose typed run metadata from `_run_snapshot`:

```python
_SAFE_STOP_REASONS = {
    "unchanged",
    "watermark_reached",
    "time_window_reached",
    "provider_exhausted",
    "budget_exhausted",
}

return {
    "id": run.id,
    "list_id": run.list_id,
    "status": run.status,
    "started_at": run.started_at,
    "finished_at": run.finished_at,
    "posts_returned": run.posts_returned,
    "truncated": run.truncated,
    "stop_reason": run.stop_reason if run.stop_reason in _SAFE_STOP_REASONS else None,
    "stages": safe_stages,
}
```

Inside safe stage snapshots, accept `truncated` only when it is a boolean and accept only a known stop reason. Do not add raw rejection identifiers to `/api/status`.

- [ ] **Step 5: Update operator documentation**

Replace the old whole-page stopping description in `README.md` with the exact defaults and semantics:

```text
Initialized Lists first perform a one-post unchanged probe. Changed or uninitialized Lists
continue in pages of 10, never request more than 50 returned posts or six pages per run,
and never backfill beyond 24 hours. Runs that exhaust a safety limit advance to the newest
collected cursor and report truncated=true so the next run does not repurchase the same history.
Individual rejected items make a run partial but no longer block cursor advancement.
```

Document all five environment names and explain that a smaller page-count override can truncate before the post budget.

- [ ] **Step 6: Run focused tests and static checks**

Run:

```bash
cd backend
uv run pytest tests/services/test_pipeline.py tests/test_observability.py tests/api/test_api.py -q
uv run ruff check src tests
uv run mypy src
```

Expected: all commands exit zero.

- [ ] **Step 7: Commit API and documentation exposure**

```bash
git add backend/src/x_digest/services/pipeline.py backend/src/x_digest/observability.py backend/src/x_digest/api/schemas.py backend/tests/services/test_pipeline.py backend/tests/test_observability.py backend/tests/api/test_api.py README.md
git commit -m "feat: expose X ingestion budget status"
```

- [ ] **Step 8: Run the full backend verification suite**

Run:

```bash
cd backend
uv run ruff check .
uv run mypy src
uv run pytest
```

Expected: Ruff and MyPy exit zero; pytest reports zero failures and zero paid-provider calls.

- [ ] **Step 9: Verify a fresh migration and downgrade/upgrade cycle**

Run against a disposable SQLite path:

```bash
cd backend
migration_db="$(mktemp -d)/x-digest-migration.sqlite3"
DATABASE_URL="sqlite+pysqlite:///$migration_db" uv run alembic upgrade head
DATABASE_URL="sqlite+pysqlite:///$migration_db" uv run alembic downgrade 0005_thread_identity
DATABASE_URL="sqlite+pysqlite:///$migration_db" uv run alembic upgrade head
```

Expected: all three Alembic commands exit zero. Do not point this command at the development or production database.

- [ ] **Step 10: Run frontend and deployment verification**

Run:

```bash
cd frontend
npm test
npm run build
cd ..
docker compose --env-file .env.example config --quiet
```

Expected: frontend tests and build exit zero; Compose configuration renders without starting services.

- [ ] **Step 11: Review the branch diff against the spec**

Run:

```bash
git status --short
git diff --stat HEAD~7..HEAD
git log --oneline -7
```

Confirm the branch contains only the seven scoped commits, all new response fields are additive, no credentials or raw provider payloads appear, and unrelated untracked workspace files remain untouched.
