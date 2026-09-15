# X List AI Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single-user, self-hosted application that monitors one or more X Lists, stores each post once, assembles readable threads, creates traceable Chinese summaries and versioned digests, and exposes them through a protected web interface with optional notifications.

**Architecture:** A FastAPI service owns the API and domain services. A separate APScheduler process calls the same services so that web-worker count cannot duplicate scheduled jobs. Both use SQLAlchemy against SQLite in local development and PostgreSQL in production. A Next.js browser-facing service acts as a backend-for-frontend (BFF): it authenticates the single operator, proxies `/api/*` to the private FastAPI service, and keeps the internal API token out of browser JavaScript.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, SQLAlchemy 2, Alembic, APScheduler, httpx, pytest, respx, Ruff, mypy, PostgreSQL 16/SQLite, Next.js with TypeScript, Playwright, Docker Compose.

**Spec:** `superpowers/specs/2026-09-11-x-list-digest-design.md`

## Global Constraints

- Single-user and self-hosted only; no registration, tenant IDs, billing, or public sharing.
- Use the official `GET /2/lists/{id}/tweets` endpoint for the first source adapter. Do not implement CAPTCHA bypass, permission bypass, browser-session collection, or automated posting.
- X `pagination_token` is page-local, not a durable incremental cursor. Every scheduled sync starts at the newest page and stops after crossing the stored watermark plus overlap; never persist `next_token` as the next run's starting point.
- Bound X cost with `X_MAX_PAGES_PER_SYNC`, `X_MAX_POSTS_PER_SYNC`, and a documented operator spending limit. Tests use fixtures only and never call X.
- Store UTC timestamps in the database. Convert to the configured IANA timezone only for digest windows, scheduling, and display.
- Every post retains `source_url`; every summary and digest item retains source post IDs. A failed fetch or model call must not delete or hide the original post.
- Idempotency keys are database-enforced: platform post, List membership, automatic summary input/model/prompt generation, explicit regeneration request, digest window/version, scheduler job ID, and digest/channel delivery.
- Automatic retries are bounded. Retry 429 and retryable 5xx/network errors with server `Retry-After` or exponential backoff plus jitter; never retry 4xx authentication/validation errors automatically.
- External-link fetching is an SSRF boundary: allow only HTTP(S), reject credentials and non-public destinations before every connection and redirect, cap redirects/body/decompressed size/time, and accept only textual content types.
- The FastAPI container is private to the Compose network. Only the Next.js service is published; it authenticates the operator and injects `INTERNAL_API_TOKEN` server-side.
- Secrets come from environment variables or mounted secret files. Do not log, return, persist, commit, or embed them in client bundles.
- Production runs exactly one scheduler container. The API process never starts an embedded scheduler.
- Pin resolved Python and Node dependencies in lock files during implementation; do not rely on floating versions in deployment.

## File and Module Map

| Area | Responsibility |
|---|---|
| `backend/src/x_digest/config.py` | Typed environment configuration and startup validation |
| `backend/src/x_digest/db.py` | Engine/session lifecycle only |
| `backend/src/x_digest/models/` | SQLAlchemy persistence models, grouped by domain |
| `backend/src/x_digest/repositories/` | Transactional queries and idempotent state transitions |
| `backend/src/x_digest/sources/` | Provider-neutral page contract and official X adapter |
| `backend/src/x_digest/services/` | Ingestion, processing, summarization, digests, and delivery |
| `backend/src/x_digest/api/` | Authenticated HTTP schemas and thin routes |
| `backend/src/x_digest/scheduler.py` | Dedicated scheduler entry point and stable job IDs |
| `frontend/src/app/api/` | Server-side authenticated BFF proxy |
| `frontend/src/app/` and `components/` | Reading UI; no backend secret handling |

## Requirement Traceability

| Spec capability | Implemented and accepted in |
|---|---|
| Multiple Lists, frequency, enable/disable, manual sync | Tasks 2, 3, 5, 10, 11 |
| Pagination, rate limits, bounded retries, partial records | Tasks 4 and 5 |
| Cross-List deduplication and source retention | Tasks 2, 3, and 5 |
| Threads, quotes/reposts, links, blacklist | Tasks 6 and 7 |
| Structured single-item summaries and cost bounds | Task 8 |
| Versioned 6-hour/daily digests | Task 9 |
| Browse/filter/read/save/ignore/regenerate | Tasks 11 and 12 |
| Optional email/Telegram, exactly-once delivery claim | Task 13 |
| Metrics, errors, deployment, end-to-end recovery | Tasks 10 and 14 |

---

### Task 1: Bootstrap a reproducible backend

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/src/x_digest/__init__.py`
- Create: `backend/src/x_digest/config.py`
- Create: `backend/src/x_digest/main.py`
- Create: `backend/src/x_digest/api/errors.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_health.py`
- Create: `backend/.env.example`
- Create: `docker-compose.yml`

**Interfaces:**
- Produces `Settings`, `create_app(settings: Settings | None = None) -> FastAPI`, `GET /health/live`, and `GET /health/ready`.
- Readiness checks database connectivity only; it does not require X or LLM credentials.

- [ ] **Step 1: Write the failing health contract.**

```python
def test_liveness_does_not_require_provider_credentials(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_readiness_reports_database_failure(client, broken_database):
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["code"] == "database_unavailable"
```

- [ ] **Step 2: Run `cd backend && python -m pytest tests/test_health.py -q`; expect collection failure because `x_digest` does not exist.**
- [ ] **Step 3: Define configuration and the app factory with these exact required settings and defaults.**

```python
class Settings(BaseSettings):
    database_url: str = "sqlite+pysqlite:///./x_digest.db"
    app_timezone: str = "UTC"
    internal_api_token: SecretStr | None = None
    x_bearer_token: SecretStr | None = None
    x_max_pages_per_sync: int = Field(default=5, ge=1, le=100)
    x_max_posts_per_sync: int = Field(default=500, ge=1, le=10_000)
    digest_cadence: Literal["6h", "daily", "both"] = "daily"
    llm_base_url: AnyHttpUrl | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_max_items_per_run: int = Field(default=100, ge=1, le=10_000)
```

- [ ] **Step 4: Implement `create_app`, exception-to-JSON mapping, and health routes; keep provider clients lazy so startup works without X/LLM credentials.**
- [ ] **Step 5: Run `cd backend && python -m pytest tests/test_health.py -q`; expect PASS.**
- [ ] **Step 6: Add lint/type commands (`ruff check .`, `mypy src`) and a backend Compose service that binds only inside the Compose network. Run `docker compose config`; expect exit 0.**
- [ ] **Step 7: Commit with `git add backend docker-compose.yml && git commit -m "chore: bootstrap x digest backend"`.**

### Task 2: Define the complete persistence schema and migrations

**Files:**
- Create: `backend/src/x_digest/db.py`
- Create: `backend/src/x_digest/models/__init__.py`
- Create: `backend/src/x_digest/models/source.py`
- Create: `backend/src/x_digest/models/content.py`
- Create: `backend/src/x_digest/models/jobs.py`
- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/versions/0001_initial.py`
- Create: `backend/tests/test_schema.py`

**Interfaces:**
- Source: `XList`, `Author`, `Post`, `PostListMembership`.
- Content: `Thread`, `ThreadPost`, `Link`, `Summary`, `Digest`, `DigestItem`, `PostState`.
- Jobs: `SyncRun`, `NotificationDelivery`.

- [ ] **Step 1: Write schema tests for all uniqueness and history guarantees.**

```python
def test_schema_idempotency_constraints(session, factories):
    post = factories.post(platform_post_id="42")
    factories.membership(post=post, list_id="list-a")
    session.commit()
    with pytest.raises(IntegrityError):
        factories.post(platform_post_id="42")
        session.commit()

def test_digest_versions_do_not_overwrite(session, factories):
    first = factories.digest(window_key="2026-09-11T00:00Z/6h", version=1)
    second = factories.digest(window_key=first.window_key, version=2)
    session.commit()
    assert first.id != second.id
```

- [ ] **Step 2: Run `cd backend && python -m pytest tests/test_schema.py -q`; expect failure for missing models.**
- [ ] **Step 3: Implement the complete initial schema. Use `String(36)` UUIDs for SQLite/PostgreSQL portability and explicit UTC-aware datetime conversion at repository boundaries.**
- [ ] **Step 4: Add these database constraints exactly:**

```text
UNIQUE posts(platform, platform_post_id)
UNIQUE post_list_memberships(post_id, list_id)
UNIQUE thread_posts(thread_id, post_id)
UNIQUE links(post_id, canonical_url)
UNIQUE summaries(content_fingerprint, model, prompt_version, generation)
UNIQUE summaries(regeneration_request_id)
UNIQUE digests(window_key, version)
UNIQUE digest_items(digest_id, source_type, source_id)
UNIQUE notification_deliveries(digest_id, channel)
CHECK summaries.importance BETWEEN 1 AND 5
```

- [ ] **Step 5: Store `latest_seen_at` and `latest_seen_post_id` on `x_lists`; store page tokens only on `sync_runs.debug_metadata` for diagnostics, never as the next-run cursor.**
- [ ] **Step 6: Create `0001_initial.py` manually from the reviewed model metadata, then run `alembic upgrade head` on a fresh SQLite database and a PostgreSQL test container.**
- [ ] **Step 7: Run `python -m pytest tests/test_schema.py -q`; expect PASS, then commit with `git add backend && git commit -m "feat: add complete persistence schema"`.**

### Task 3: Implement transactional repositories and idempotency

**Files:**
- Create: `backend/src/x_digest/repositories/lists.py`
- Create: `backend/src/x_digest/repositories/posts.py`
- Create: `backend/src/x_digest/repositories/processing.py`
- Create: `backend/src/x_digest/repositories/jobs.py`
- Create: `backend/src/x_digest/repositories/types.py`
- Create: `backend/tests/repositories/test_posts.py`
- Create: `backend/tests/repositories/test_jobs.py`

**Interfaces:**
- `PostUpsert` is the provider-neutral repository input DTO defined in `repositories/types.py`.
- `upsert_post(session, post: PostUpsert) -> tuple[Post, bool]` where the Boolean is `created`.
- `attach_to_list(session, post_id: str, list_id: str) -> bool` where `False` means the membership already existed.
- `begin_sync_run`, `finish_sync_run`, `fail_sync_run`, `advance_list_watermark`.
- `claim_summary`, `claim_digest_version`, and `claim_delivery` return an existing record on idempotency conflict. Automatic summaries use `generation=1`; an explicit regeneration allocates the next generation under a caller-supplied idempotency key.

- [ ] **Step 1: Write a concurrency-style repository test using two sessions: both insert post `42`; exactly one row and two valid List memberships remain after conflict recovery.**
- [ ] **Step 2: Write state-transition tests proving a failed sync cannot advance a List watermark and a sent notification cannot transition back to pending.**
- [ ] **Step 3: Run `cd backend && python -m pytest tests/repositories -q`; expect failures.**
- [ ] **Step 4: Define `PostUpsert` with platform ID, author, text, UTC creation time, source URL, conversation/reply/reference IDs, entities, and media metadata; then implement repositories with one transaction owner per service call. Repository functions may flush but must not commit internally.**
- [ ] **Step 5: Implement conflict handling with database uniqueness plus savepoints; do not use check-then-insert as the sole concurrency guard.**
- [ ] **Step 6: Run repository tests on SQLite and PostgreSQL; expect identical externally visible results.**
- [ ] **Step 7: Commit with `git add backend && git commit -m "feat: add idempotent repositories"`.**

### Task 4: Define the source contract and official X adapter

**Files:**
- Create: `backend/src/x_digest/sources/base.py`
- Create: `backend/src/x_digest/sources/types.py`
- Create: `backend/src/x_digest/sources/errors.py`
- Create: `backend/src/x_digest/sources/x_api.py`
- Create: `backend/tests/fixtures/x/list_posts_page_1.json`
- Create: `backend/tests/fixtures/x/list_posts_partial.json`
- Create: `backend/tests/sources/test_x_api.py`
- Create: `backend/scripts/check_x_access.py`

**Interfaces:**

```python
class XSource(Protocol):
    async def fetch_page(
        self, *, list_id: str, pagination_token: str | None, max_results: int
    ) -> SourcePage: ...

@dataclass(frozen=True)
class SourcePage:
    posts: tuple[RawPost, ...]
    rejected_items: tuple[RejectedItem, ...]
    next_token: str | None
```

- [ ] **Step 1: Write fixture-backed tests that assert the request path, `max_results <= 100`, requested post/user/media fields, expansion parsing, `next_token`, and per-item rejection.**
- [ ] **Step 2: Add retry tests: 429 honors `Retry-After`; 500/network errors use injected sleep and bounded exponential backoff; 401/403 fail immediately; cancellation is re-raised.**
- [ ] **Step 3: Run `cd backend && python -m pytest tests/sources/test_x_api.py -q`; expect failure.**
- [ ] **Step 4: Implement `XApiSource` with an injected `httpx.AsyncClient`, injected sleeper, and typed `AuthenticationError`, `RateLimitError`, and `UpstreamError`.**
- [ ] **Step 5: Request the exact fields needed downstream: `created_at,author_id,conversation_id,in_reply_to_user_id,referenced_posts,entities,attachments,lang` plus author/media expansions.**
- [ ] **Step 6: Implement `scripts/check_x_access.py` as an opt-in operator preflight: fetch one item, print status/result count/request IDs but no content or token, and exit nonzero for auth, credit, or permission failure.**
- [ ] **Step 7: Run adapter tests with network disabled; expect PASS. Do not run the preflight in CI.**
- [ ] **Step 8: Commit with `git add backend && git commit -m "feat: add official x list source"`.**

### Task 5: Implement bounded incremental ingestion

**Files:**
- Create: `backend/src/x_digest/services/ingestion.py`
- Create: `backend/src/x_digest/services/normalization.py`
- Create: `backend/tests/services/test_ingestion.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class SyncResult:
    run_id: str
    status: Literal["succeeded", "partial", "failed"]
    pages_fetched: int
    posts_seen: int
    posts_created: int
    duplicates: int
    rejected: int

class IngestionService:
    async def sync_list(self, list_id: str) -> SyncResult: ...
```

- [ ] **Step 1: Write the first-sync test: consume pages from newest to oldest, stop at the configured page/post cap, persist valid items, and report malformed items without aborting the batch.**
- [ ] **Step 2: Write the incremental test: start again with `pagination_token=None`, continue until every item on a page is older than `latest_seen_at - overlap`, and avoid duplicate rows and memberships.**
- [ ] **Step 3: Write failure tests: a page-level upstream failure marks the run failed/partial, records a redacted error, and does not advance the watermark; a retry then safely replays stored items.**
- [ ] **Step 4: Run `cd backend && python -m pytest tests/services/test_ingestion.py -q`; expect failures.**
- [ ] **Step 5: Implement normalization from `RawPost` to the Task 3 `PostUpsert` DTO for authors, media metadata, replies, quotes, reposts, URLs, and source URL. Treat API `errors` beside valid `data` as rejected items rather than discarding the valid page.**
- [ ] **Step 6: Implement the stop policy with a default 10-minute overlap. Advance the watermark only after all requested pages complete without a page-level failure, using the maximum `(created_at, platform_post_id)` observed.**
- [ ] **Step 7: Run the ingestion and repository suites; expect PASS.**
- [ ] **Step 8: Commit with `git add backend && git commit -m "feat: add bounded incremental ingestion"`.**

### Task 6: Assemble threads, relations, links, and filters

**Files:**
- Create: `backend/src/x_digest/services/threading.py`
- Create: `backend/src/x_digest/services/link_extraction.py`
- Create: `backend/src/x_digest/services/filters.py`
- Create: `backend/tests/services/test_threading.py`
- Create: `backend/tests/services/test_link_extraction.py`
- Create: `backend/tests/services/test_filters.py`

**Interfaces:**
- `assemble_threads(posts: Sequence[PostView]) -> tuple[ThreadCandidate, ...]`.
- `extract_links(post: PostView) -> tuple[ExtractedLink, ...]`.
- `evaluate_filter(post: PostView, rules: FilterRules) -> FilterDecision`.

- [ ] **Step 1: Write thread tests for an explicit reply chain arriving out of order, a missing root, two authors in one conversation, a quote that remains separate, and a reply cycle that is rejected.**
- [ ] **Step 2: Write link tests for `expanded_url`, duplicate tracking parameters, X internal status URLs, invalid schemes, and stable canonical order.**
- [ ] **Step 3: Write filter tests with the default policy: include original posts and replies, exclude pure reposts, mark but do not delete suspected ads, and let explicit blacklist rules win.**
- [ ] **Step 4: Run the three targeted files; expect failures.**
- [ ] **Step 5: Implement thread grouping from explicit `replied_to` references first and `conversation_id + author_id` second. Do not merge merely because two posts by one author are close in time.**
- [ ] **Step 6: Implement canonicalization that lowercases hostnames, removes fragments/default ports and configured tracking parameters, but preserves path and non-tracking query semantics.**
- [ ] **Step 7: Persist deterministic thread membership/order and filter reason codes; repeated processing must yield no new rows.**
- [ ] **Step 8: Run targeted and full backend tests; expect PASS, then commit with `git add backend && git commit -m "feat: process threads links and filters"`.**

### Task 7: Fetch external text behind an SSRF boundary

**Files:**
- Create: `backend/src/x_digest/services/link_content.py`
- Create: `backend/src/x_digest/security/network.py`
- Create: `backend/tests/services/test_link_content.py`
- Create: `backend/tests/security/test_network.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class LinkContentResult:
    status: Literal["fetched", "unsupported", "blocked", "failed", "too_large"]
    final_url: str
    title: str | None
    text: str | None
    error_code: str | None
```

- [ ] **Step 1: Write rejection tests for loopback, RFC1918, link-local, multicast, `.local`, URL credentials, non-HTTP schemes, DNS rebinding, and a redirect from public to private IP.**
- [ ] **Step 2: Write success/failure tests for HTML/text, unsupported MIME type, timeout, redirect loop, compressed body over the decompressed limit, and extraction that returns no useful text.**
- [ ] **Step 3: Run the targeted tests; expect failures.**
- [ ] **Step 4: Implement URL validation before every request/redirect and pin each connection to a validated public resolution. Set defaults: 3 redirects, 10 seconds total, 2 MiB compressed, 5 MiB decompressed, and 200,000 extracted characters.**
- [ ] **Step 5: Implement deterministic HTML title/body extraction. Store status and sanitized error code; never persist response headers, cookies, credentials, or raw binary bodies.**
- [ ] **Step 6: Run security and link-content tests; expect PASS, then commit with `git add backend && git commit -m "feat: safely extract linked content"`.**

### Task 8: Generate validated and idempotent summaries

**Files:**
- Create: `backend/src/x_digest/ai/contracts.py`
- Create: `backend/src/x_digest/ai/client.py`
- Create: `backend/src/x_digest/ai/prompts.py`
- Create: `backend/src/x_digest/services/summarization.py`
- Create: `backend/tests/ai/test_contracts.py`
- Create: `backend/tests/services/test_summarization.py`

**Interfaces:**

```python
class SummaryOutput(BaseModel):
    summary: str = Field(min_length=1)
    key_points: list[str]
    topics: list[str]
    importance: int = Field(ge=1, le=5)
    language: str = Field(min_length=2)
    source_ids: list[str] = Field(min_length=1)

class Summarizer:
    async def summarize(self, content: NormalizedContent) -> SummaryOutput: ...
```

`NormalizedContent` represents one readable unit: a complete available thread when thread membership exists, otherwise one post. It contains ordered `source_ids`, source text, and successfully extracted link text; failed links contribute their URL and failure status but no invented body text.

- [ ] **Step 1: Write contract tests for valid structured output, invalid JSON, missing/extra fields, blank summary, out-of-range importance, and source IDs not contained in the input.**
- [ ] **Step 2: Write service tests for timeout, retryable provider failure, permanent validation failure, partial link-fetch failure, and two workers claiming the same summary key.**
- [ ] **Step 3: Run targeted tests; expect failures.**
- [ ] **Step 4: Implement a provider-neutral async client configured by base URL/model/API key, with bounded timeout, maximum output tokens, at most two retries, and secret-safe error mapping.**
- [ ] **Step 5: Define `PROMPT_VERSION = "single-content-v1"`; hash normalized source text, extracted link text, ordered source IDs, model, and prompt version into the content fingerprint. Automatic work claims generation 1; manual regeneration creates generation N+1 and deduplicates retries with `regeneration_request_id`.**
- [ ] **Step 6: Persist attempt count, token usage when returned, estimated cost when configured, failure code, model, prompt version, and timestamps. Never log prompt content at INFO level.**
- [ ] **Step 7: Run tests with a fake provider and network disabled; expect PASS, then commit with `git add backend && git commit -m "feat: add structured summaries"`.**

### Task 9: Build topic-grouped, versioned digests

**Files:**
- Create: `backend/src/x_digest/services/digests.py`
- Create: `backend/tests/services/test_digests.py`

**Interfaces:**
- `window_for(now: datetime, cadence: Literal["6h", "daily"], tz: ZoneInfo) -> DigestWindow`.
- `build_digest(window: DigestWindow, *, regenerate: bool = False) -> Digest`.

- [ ] **Step 1: Write window tests across UTC offset changes and local midnight; persist the canonical UTC `[start, end)` boundaries and configured timezone.**
- [ ] **Step 2: Write digest tests for topic grouping, stable importance/time ordering, unsummarized-source fallback, empty windows, repeated automatic calls, and explicit regeneration.**
- [ ] **Step 3: Assert automatic calls return the existing version, while `regenerate=True` creates `version + 1` and leaves the previous digest/items unchanged.**
- [ ] **Step 4: Run `cd backend && python -m pytest tests/services/test_digests.py -q`; expect failures.**
- [ ] **Step 5: Implement deterministic grouping from normalized topic slugs. Store a rendered snapshot plus normalized `DigestItem` rows so old versions cannot change when summaries are later regenerated.**
- [ ] **Step 6: Run targeted tests; expect PASS, then commit with `git add backend && git commit -m "feat: add versioned digests"`.**

### Task 10: Add the dedicated scheduler and operational telemetry

**Files:**
- Create: `backend/src/x_digest/services/pipeline.py`
- Create: `backend/src/x_digest/scheduler.py`
- Create: `backend/src/x_digest/observability.py`
- Create: `backend/tests/services/test_pipeline.py`
- Create: `backend/tests/test_scheduler.py`
- Create: `backend/tests/test_observability.py`
- Modify: `docker-compose.yml`

**Interfaces:**
- `run_list_pipeline(list_id: str) -> PipelineResult` runs ingestion, deterministic processing, pending link extraction, and bounded summary work in that order while recording each stage result.
- `build_scheduler(settings, services) -> BlockingScheduler`.
- Stable IDs: `sync-list:{list_id}`, configured `digest:6h` and/or `digest:daily`, and `notify:{digest_id}`.

- [ ] **Step 1: Write pipeline tests proving a newly ingested thread reaches summary persistence, one failed link does not block the summary, the per-run LLM item cap is enforced, and retryable work is picked up on the next run.**
- [ ] **Step 2: Write scheduler tests for enabled/disabled Lists, interval changes, stable IDs with `replace_existing=True`, coalescing, one concurrent instance per List, and only the configured timezone-aware digest cadence(s).**
- [ ] **Step 3: Write telemetry tests that a completed pipeline exposes per-stage counts/duration/last error without exposing provider payloads or secrets.**
- [ ] **Step 4: Run targeted tests; expect failures.**
- [ ] **Step 5: Implement `run_list_pipeline` with independent stage transactions so a summary/provider failure cannot roll back ingested originals. Select only new/changed/pending units and enforce `llm_max_items_per_run`.**
- [ ] **Step 6: Implement a foreground scheduler entry point. Configure `coalesce=True`, `max_instances=1`, and a bounded misfire grace time; reconcile List jobs periodically from database configuration.**
- [ ] **Step 7: Add structured JSON logs with `run_id`, `list_id`, `digest_id`, duration, counts, and stable error codes. Provide authenticated `/api/status` data from stored run/summary/delivery records.**
- [ ] **Step 8: Add exactly one `scheduler` service to Compose using the same image/database as the API. Verify the API command does not import or start APScheduler.**
- [ ] **Step 9: Run tests and `docker compose config`; expect PASS/exit 0, then commit with `git add backend docker-compose.yml && git commit -m "feat: schedule pipelines and report status"`.**

### Task 11: Expose a protected backend API

**Files:**
- Create: `backend/src/x_digest/api/auth.py`
- Create: `backend/src/x_digest/api/schemas.py`
- Create: `backend/src/x_digest/api/lists.py`
- Create: `backend/src/x_digest/api/posts.py`
- Create: `backend/src/x_digest/api/digests.py`
- Create: `backend/src/x_digest/api/jobs.py`
- Create: `backend/src/x_digest/api/status.py`
- Create: `backend/tests/api/test_auth.py`
- Create: `backend/tests/api/test_api.py`
- Modify: `backend/src/x_digest/main.py`

**Interfaces:**
- `GET/POST /api/lists`; `PATCH/DELETE /api/lists/{id}`.
- `GET /api/posts?list_id=&topic=&author=&from=&to=&state=&cursor=&limit=`.
- `POST /api/posts/{id}/state` with `{"read": bool?, "saved": bool?, "ignored": bool?}`.
- `GET /api/digests`; `GET /api/digests/{id}`; `POST /api/digests/{id}/regenerate`.
- `POST /api/jobs/sync/{list_id}` runs one bounded pipeline and returns its `PipelineResult`; `POST /api/posts/{id}/summaries/regenerate` requires an `Idempotency-Key` header; `GET /api/status`.

- [ ] **Step 1: Write auth tests: health routes work anonymously; every `/api/*` route returns 401 without `Authorization: Bearer <INTERNAL_API_TOKEN>` and succeeds with a constant-time-validated token.**
- [ ] **Step 2: Write API tests for List validation, cursor pagination with stable ordering, combined filters, source URL presence, state mutation idempotency, digest version retrieval, summary regeneration deduplicated by `Idempotency-Key`, and missing-resource errors.**
- [ ] **Step 3: Run `cd backend && python -m pytest tests/api -q`; expect failures.**
- [ ] **Step 4: Implement strict request/response models and thin routes that call services. Standardize errors as `{"code": str, "message": str, "request_id": str}` and never return exception text.**
- [ ] **Step 5: Reject startup in non-test mode if `INTERNAL_API_TOKEN` is absent or shorter than 32 random bytes. Do not add permissive CORS because the browser uses the same-origin BFF.**
- [ ] **Step 6: Run the full backend suite and snapshot/check the OpenAPI paths against the interface list; expect PASS.**
- [ ] **Step 7: Commit with `git add backend && git commit -m "feat: expose protected digest api"`.**

### Task 12: Build the authenticated reading interface and BFF

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/package-lock.json`
- Create: `frontend/next.config.ts`
- Create: `frontend/src/middleware.ts`
- Create: `frontend/src/app/api/[...path]/route.ts`
- Create: `frontend/src/app/page.tsx`
- Create: `frontend/src/app/posts/page.tsx`
- Create: `frontend/src/app/digests/[id]/page.tsx`
- Create: `frontend/src/components/FilterBar.tsx`
- Create: `frontend/src/components/PostCard.tsx`
- Create: `frontend/src/components/SummaryPanel.tsx`
- Create: `frontend/src/lib/api.ts`
- Create: `frontend/tests/reading-flow.spec.ts`
- Modify: `docker-compose.yml`

**Interfaces:**
- Browser calls same-origin `/api/*`; the route handler forwards to `BACKEND_INTERNAL_URL` and injects `INTERNAL_API_TOKEN` only on the server.
- Middleware protects all UI/BFF routes with operator credentials from environment settings; health/static assets are the only exclusions.

- [ ] **Step 1: Write Playwright tests with a mocked FastAPI upstream for authentication, digest list/detail, post stream, URL-backed filters, source-link presence, read/save/ignore, regeneration, and loading/empty/error states.**
- [ ] **Step 2: Add a build-time test that scans emitted client assets and fails if the configured internal API token appears.**
- [ ] **Step 3: Run `cd frontend && npm test`; expect failure because the app is absent.**
- [ ] **Step 4: Scaffold strict TypeScript, lock dependencies, and implement server-side proxy/auth. Forward only allowlisted methods/headers; set request timeouts and propagate the backend request ID.**
- [ ] **Step 5: Implement digest and post pages. Keep `list_id`, `topic`, `author`, `from`, `to`, and `state` in the URL; render summary beside original content and always show the original X link.**
- [ ] **Step 6: Implement accessible action controls with optimistic UI rollback and visible failure messages. Never render link HTML or model output as unsanitized HTML.**
- [ ] **Step 7: Run `npm test` and `npm run build`; expect PASS. Add only the frontend service to the host port in Compose.**
- [ ] **Step 8: Commit with `git add frontend docker-compose.yml && git commit -m "feat: add protected reading interface"`.**

### Task 13: Deliver optional email and Telegram notifications once

**Files:**
- Create: `backend/src/x_digest/notifications/base.py`
- Create: `backend/src/x_digest/notifications/email.py`
- Create: `backend/src/x_digest/notifications/telegram.py`
- Create: `backend/src/x_digest/services/notification_delivery.py`
- Create: `backend/tests/notifications/test_delivery.py`
- Modify: `backend/src/x_digest/scheduler.py`
- Modify: `backend/.env.example`

**Interfaces:**

```python
class Notifier(Protocol):
    async def send(self, message: DigestMessage) -> ProviderReceipt: ...

class NotificationDeliveryService:
    async def deliver_once(self, digest_id: str, channel: str) -> DeliveryResult: ...
```

- [ ] **Step 1: Write tests for disabled channels, successful delivery, transient retry, permanent provider rejection, process death after provider acceptance, and two workers claiming one digest/channel.**
- [ ] **Step 2: Run targeted tests; expect failures.**
- [ ] **Step 3: Implement a database claim state machine `pending -> sending -> sent|retryable_failed|permanent_failed`, with attempt count, next-attempt time, provider receipt, and stale-claim recovery.**
- [ ] **Step 4: Document the delivery semantic honestly as at-most-one successful application claim with possible provider-level duplicate after an ambiguous timeout; include `digest_id` in subject/body for human deduplication.**
- [ ] **Step 5: Implement SMTP and Telegram adapters behind optional settings. Escape channel markup, cap message size, and link to the web digest when truncation is required.**
- [ ] **Step 6: Register delivery jobs only after a persisted digest version exists; retries reuse the same delivery row and never create a new digest.**
- [ ] **Step 7: Run targeted/full tests; expect PASS, then commit with `git add backend && git commit -m "feat: add digest notifications"`.**

### Task 14: Verify deployment, recovery, and the complete user journey

**Files:**
- Create: `README.md`
- Create: `backend/tests/e2e/test_fake_provider_flow.py`
- Create: `frontend/tests/compose-flow.spec.ts`
- Create: `.github/workflows/ci.yml`
- Modify: `backend/.env.example`
- Modify: `docker-compose.yml`

**Interfaces:**
- Fake X, link, LLM, and notifier providers are selectable only when `APP_ENV=test`; production startup rejects fake-provider settings.

- [ ] **Step 1: Write the backend end-to-end test: create two Lists, sync overlapping fixtures, assert one post/two memberships, assemble a thread, tolerate one failed link, summarize, generate two digest versions, and claim one notification.**
- [ ] **Step 2: Write the Compose browser journey: authenticate, create List, trigger sync, read original and summary, filter, save, open digest, regenerate, and verify both versions remain accessible.**
- [ ] **Step 3: Add CI jobs for backend lint/type/tests, frontend tests/build, Alembic fresh-upgrade, secret scan, and `docker compose config`. No CI job may require paid/provider credentials.**
- [ ] **Step 4: Document exact local startup, secret generation, migration, X access preflight, X read/spending caps, model cost controls, notification setup, backup/restore, upgrade, scheduler singleton, logs/status, and failure recovery.**
- [ ] **Step 5: Run the verification commands below from a clean checkout with fake providers. Record the command and passing output in the implementation handoff.**
- [ ] **Step 6: Stop the database during a sync, restart it, rerun the job, and verify no post, summary, digest, or notification claim is duplicated.**
- [ ] **Step 7: Commit with `git add README.md backend frontend .github docker-compose.yml && git commit -m "docs: verify x digest deployment"`.**

## Final Verification Commands

```bash
cd backend && ruff check .
cd backend && mypy src
cd backend && python -m pytest -q
cd frontend && npm test
cd frontend && npm run build
docker compose config
docker compose up --build -d
cd frontend && npm run test:e2e
docker compose down
```

## Release Acceptance

- A post returned in two monitored Lists occupies one `posts` row and two membership rows.
- Re-running any sync, summary, automatic digest, scheduler reconciliation, or delivery claim is externally idempotent.
- A partial X response retains valid posts and records rejected items; a page-level failure does not advance the List watermark.
- Thread order is deterministic and quotes/reposts are not silently merged into reply threads.
- Link failures and AI failures leave original posts visible with stable, non-secret error codes.
- Every summary/digest item exposes its source IDs and original X links.
- Regeneration creates a new immutable digest version.
- The browser bundle contains no backend/provider credential; unauthenticated requests cannot reach reading or job APIs.
- Only one scheduler process exists in the documented production topology.
- The complete fake-provider journey passes without internet access or paid credentials.

## Known External Gate

Before investing in frontend work, the operator must run `backend/scripts/check_x_access.py` with their own developer credentials and confirm that the target List is accessible and the current X Developer Console budget is acceptable. API availability and pricing are external facts and are not guaranteed by this repository.
