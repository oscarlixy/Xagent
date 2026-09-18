# X Digest Production Readiness Repair Design

## Goal

Complete the existing X Digest MVP in the agreed priority order without introducing new infrastructure or unrelated abstractions. The result must support current X List responses, bounded incremental collection, database-safe concurrent execution, bounded link work, useful immutable digests, optional notifications, and automated verification.

## Scope and order

1. Align the X List adapter with the current `post.fields` and `referenced_posts` contract while accepting legacy `referenced_tweets` fixtures during transition.
2. Make every synchronization begin at the newest page and stop once a complete page is older than a ten-minute overlap behind the stored watermark. Keep page and post caps as hard safety limits.
3. Recover from unique-key races for posts, memberships, threads, summaries, and digests. Deleting a List removes its memberships and sync history without deleting shared posts.
4. Move network link fetching outside database transactions. Process a configurable finite batch and persist attempt count, next attempt time, and a terminal state after the retry limit.
5. Build deterministic topic-grouped digest snapshots. Include successfully summarized units and a source-preserving fallback for unsummarized posts; regeneration creates a new immutable version.
6. Add optional SMTP and Telegram delivery using the existing `notification_deliveries` table, with bounded retries and stale-claim recovery. Disabled channels create no work.
7. Add a minimal CI workflow for backend lint, typing, tests, migrations, frontend tests/build, secret scanning, and Compose configuration.

## Architecture

The existing FastAPI, SQLAlchemy, APScheduler, Next.js, and Docker Compose topology remains unchanged. PostgreSQL remains the concurrency authority through unique constraints and short transactions. Provider calls happen outside database transactions; database rows carry enough state for a later scheduler run to resume safely.

The scheduler remains a single documented process, but services must still tolerate overlap between manual API jobs and per-List scheduled jobs. No Redis, Celery, external queue, or additional service is introduced.

## Data flow and failure behavior

### X collection

Each run loads the List watermark, requests the newest page, stores valid posts idempotently, and advances through pagination. On later runs it stops after a page whose valid posts are all older than `watermark - 10 minutes`. It advances the watermark only after a page-level-successful traversal. Partial item errors retain valid posts and stable redacted error codes.

### Concurrent persistence

Insert paths use uniqueness as the final guard and recover the winning row after an integrity conflict. List deletion explicitly removes dependent `sync_runs` and memberships before deleting the List. Shared authors, posts, summaries, and digests remain intact.

### Link work

The pipeline first claims a limited set of eligible links in a short transaction, fetches each URL without an open transaction, and then records each result in a second short transaction. Retryable failures use bounded exponential delay; exhausted failures become terminal. Existing SSRF, redirect, body-size, and timeout protections remain unchanged.

### Summaries and digests

Summary input is truncated deterministically to a configurable character budget. Provider calls have bounded immediate retries, and every failed automatic attempt is represented by a persisted Summary row with safe failure metadata. Usage is stored when provided by the provider.

Digest generation remains deterministic and does not add a second LLM call. It groups immutable item snapshots by normalized primary topic, orders groups and items stably, fills `rendered_content`, and includes unsummarized posts from the window as fallback items with original source IDs and URLs.

### Notifications

After a digest is persisted, enabled channels obtain one delivery row per digest and channel. A claim transitions through `pending/retryable_failed -> sending -> sent|retryable_failed|permanent_failed`. Stale `sending` claims can be retried. SMTP and Telegram adapters receive an already-rendered plain-text message and never render untrusted HTML.

## Configuration

New settings use conservative defaults and remain optional:

- `INGESTION_OVERLAP_MINUTES=10`
- `LINK_MAX_ITEMS_PER_RUN=20`
- `LINK_MAX_ATTEMPTS=3`
- `SUMMARY_MAX_INPUT_CHARS=120000`
- notification channel credentials and enable flags

Missing notification credentials disable that channel. Existing deployments continue to start with deterministic summaries and notifications disabled.

## Verification

Each stage begins with focused regression tests. Final acceptance requires:

- backend pytest, Ruff, and MyPy pass;
- frontend build, asset-secret scan, and Playwright tests pass;
- fresh Alembic upgrade succeeds;
- Compose configuration renders successfully;
- fake-provider tests prove overlap stopping, concurrent conflict recovery, bounded retries, immutable digest versions, and notification deduplication;
- no test requires paid X or OpenAI credentials.

Real X access and pricing remain an operator-controlled external preflight and are not exercised automatically.
