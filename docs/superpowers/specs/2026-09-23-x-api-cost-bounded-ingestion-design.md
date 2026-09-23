# Cost-Bounded X List Ingestion Design

## Goal

Reduce the cost of each X List synchronization without changing the product from a List-based feed or silently claiming completeness. Steady-state reads should stay close to the number of new posts, an inactive List should require only a one-post probe, and every run must have a hard budget of 50 returned posts. Initial synchronization and recovery after a long outage may inspect only the most recent 24 hours. If that window contains more than the budget allows, the system must preserve the newest data, advance safely, and report the possible omission explicitly.

This design accepts two deliberate completeness trade-offs:

- Posts older than the 24-hour recovery window are not backfilled.
- When a run reaches the 50-post budget before reaching its time or watermark boundary, older posts may be omitted. The run is marked as truncated and does not retry the same history automatically.

## Provider constraint

The current `GET /2/lists/{id}/tweets` endpoint supports `max_results`, `pagination_token`, fields, and expansions, but not `since_id` or `start_time`. The application therefore cannot enforce the incremental boundary at the provider. It must minimize the returned resources through a one-post head probe, small pages, local boundary checks, and a strict total-result budget.

The application continues to use the List Posts endpoint. Replacing it with Recent Search would require synchronizing List membership, maintaining author queries, and accepting potentially different timeline semantics. Streaming or webhook infrastructure is not justified by the current volume.

## Synchronization boundaries

Every run computes an effective lower time boundary:

```text
recovery_cutoff = now - 24 hours
watermark_cutoff = latest_seen_at - 10 minute overlap, when a watermark exists
effective_cutoff = max(recovery_cutoff, watermark_cutoff)
```

For an uninitialized List, `effective_cutoff` is only `recovery_cutoff`. The clock used to calculate the cutoff is captured once at the start of the run so the boundary cannot move during pagination.

Only successfully parsed posts whose `created_at` is at or after `effective_cutoff` are persisted. A page containing at least one valid post older than the cutoff is sufficient to stop pagination after the page is durably processed. The List timeline is consumed newest-first, so the application no longer requires an entire page to be older than the boundary.

Rejected items do not provide a reliable timestamp and therefore cannot prove that the time boundary has been reached. They are recorded but do not by themselves trigger a boundary stop.

## Read algorithm and budget

An initialized List first requests one result from the newest page. The first raw data item supplies the provider head ID.

1. If the returned head ID equals the stored `latest_observed_post_id`, the run stops with reason `unchanged`.
2. If the head differs, the probe result is processed and pagination continues from its `next_token` in pages of at most 10 returned posts.
3. Each later request sets `max_results` to the smaller of 10 and the remaining post budget.
4. The run stops when it crosses the effective cutoff, reaches the stored watermark, exhausts provider pagination, reaches a request safety limit, or consumes the 50-post budget.
5. All provider data items count against the budget, including malformed items that are rejected locally. Provider error objects that do not represent returned data items are recorded as rejections but do not count as returned posts.

For a List without `latest_observed_post_id`, including existing Lists immediately after migration, the first request uses the normal page size of 10 rather than a one-result probe. It establishes the head ID while avoiding an extra initialization request. Subsequent pages remain capped at 10 and the total remains capped at 50.

The default request safety limit is six pages. This permits a steady-state sequence of `1, 10, 10, 10, 10, 9`, while initial synchronization needs no more than five ten-result pages. If an operator configures a smaller request limit, reaching it before a natural boundary is also truncation.

Expected upper bounds with default settings are:

- no new posts: 1 returned post;
- 1–9 new posts: at most 11 returned posts;
- about 30 new posts: at most 41 returned posts;
- any run: at most 50 returned posts.

These are resource-return bounds, not billing guarantees. X pricing, expansion charging, and same-day resource deduplication remain provider-controlled.

## Cursor model

The List keeps two distinct cursor concepts:

- `latest_observed_post_id` is the first raw data item returned by the provider during the most recent safely completed run. It supports the one-result unchanged probe even when later local processing is partial.
- `latest_seen_at` and `latest_seen_post_id` identify the newest valid post successfully parsed and persisted. They remain the business watermark and never move backward.

The source adapter exposes `head_post_id` separately from parsed posts so a malformed first item can still establish the provider head when its raw ID is a string. An errors-only response without a raw data item cannot establish a new head.

The two cursor concepts are updated only in the final completion transaction. Page-level post writes may commit earlier, but a failed run never advances either cursor. A later retry can therefore reread already persisted pages safely through existing idempotent post and membership writes.

On a safely completed `succeeded` or `partial` run, `latest_observed_post_id` advances to the observed head when available. The valid-post watermark advances to the maximum valid `(created_at, post_id)` seen by the run when that value is newer than the stored watermark. A truncated run follows the same cursor rules because intentional omission at the budget boundary has been accepted; it must not repeatedly buy the same history.

## Outcome and completeness model

Run health and collection completeness are separate dimensions:

```text
status: running | succeeded | partial | failed
truncated: boolean
stop_reason:
  unchanged
  watermark_reached
  time_window_reached
  provider_exhausted
  budget_exhausted
```

`partial` means one or more individual items were rejected while the overall traversal ended safely. It no longer blocks cursor advancement. A rejected item is not retried automatically after a safe completion; its identifier and safe reason remain available for diagnosis or a future explicit repair workflow.

`truncated=true` means the post or request budget was exhausted while a provider `next_token` remained and neither the watermark nor time boundary had been reached. Truncation may coexist with either `succeeded` or `partial`. A failed run does not use truncation to imply safe completion and does not advance cursors.

`stop_reason` records the decisive termination condition. When several conditions become true after the same page, natural completeness takes precedence over budget exhaustion in this order: `watermark_reached`, `time_window_reached`, `provider_exhausted`, then `budget_exhausted`. An unchanged probe always reports `unchanged`.

## Persistence changes

Add nullable `latest_observed_post_id` to `x_lists`.

Add the following fields to `sync_runs`:

- `posts_returned`, non-null integer with default zero;
- `truncated`, non-null boolean with default false;
- `stop_reason`, nullable string.

Add `sync_rejections` with:

- a generated primary key;
- `sync_run_id` foreign key;
- nullable provider item identifier;
- a safe, bounded reason code;
- creation timestamp.

`posts_returned` is derived from the number of raw `data` items, not the number of valid parsed posts. Existing `posts_seen`, `posts_created`, `duplicates`, and `rejected` retain their current meanings. Rejection rows are bounded by the same per-run resource and request limits.

The final transaction atomically persists the terminal run metadata and any cursor updates. If it rolls back, best-effort failure handling marks the durable run failed without advancing the List cursors.

## Source and service interfaces

`SourcePage` gains:

- `returned_count`, the count of raw provider data items;
- `head_post_id`, the first raw data item's string ID when available.

`XApiSource` populates these fields before filtering invalid items. Fake sources and source tests must model them explicitly where cursor or budget behavior matters.

`IngestionService` accepts page size, post budget, page safety limit, overlap, backfill window, and an injectable clock. It owns cutoff computation, pagination, cost accounting, stop-reason selection, rejection persistence, and the final cursor decision. Provider requests remain outside active database transactions.

Downstream processing continues after `succeeded` and `partial` ingestion and operates only on valid persisted posts. Truncation is reported but does not prevent processing of the collected subset.

## Configuration

Use these defaults:

```text
X_INGESTION_BACKFILL_HOURS=24
X_INGESTION_OVERLAP_MINUTES=10
X_PAGE_SIZE=10
X_MAX_POSTS_PER_SYNC=50
X_MAX_PAGES_PER_SYNC=6
```

`X_MAX_POSTS_PER_SYNC` is the primary cost guard. `X_MAX_PAGES_PER_SYNC` remains as a secondary request safety guard for compatibility with existing deployments. Both apply independently, and exhausting either before a natural boundary sets truncation. Validation requires positive page size and budgets, page size no greater than the provider maximum of 100, and a backfill window greater than zero.

Existing deployments that explicitly set a smaller page limit remain safe but may truncate earlier. Documentation must explain that relationship rather than silently overriding operator configuration.

## API and observability

The public status enum remains `succeeded | partial | failed`, avoiding a breaking new enum value. Ingestion results, pipeline responses, structured completion logs, and `/api/status` expose:

- `posts_returned`;
- `truncated`;
- `stop_reason`;
- existing page, valid-post, duplicate, creation, and rejection counts.

The API additions are additive. `truncated` is a boolean, not encoded as a count or inferred from `pages_fetched`. Safe rejection details remain queryable from persistence; normal status responses expose counts and reason codes but not raw upstream error bodies.

Operational diagnosis should be able to distinguish an unchanged one-read run, a complete incremental run, a 24-hour recovery stop, and an intentional budget truncation without inspecting application internals.

## Migration and compatibility

An Alembic migration adds the new columns and rejection table with server-safe defaults for existing rows. Existing Lists begin with no observed-head cursor. Their first post-migration run uses ten-result pages, respects their current watermark when recent, applies the 24-hour cap when the watermark is old or absent, and establishes `latest_observed_post_id` only after safe completion.

No new infrastructure, queue, scheduler, or X permission is introduced. The scheduler continues to create one job for every enabled List, and manual List synchronization retains the same endpoint and scope.

## Verification

Focused tests must prove:

- an unchanged initialized List makes one one-result source call and stops;
- an uninitialized List starts with a ten-result page rather than an additional probe;
- 1–9 new posts return no more than 11 total items;
- approximately 30 new posts return no more than 41 total items;
- first synchronization and long-outage recovery persist no valid post older than 24 hours;
- a mixed new/old page stops immediately after durable processing without fetching another page;
- all raw data items, including locally rejected ones, count toward the 50-post budget;
- reaching the post or page budget with remaining pagination sets `truncated=true`, records `budget_exhausted`, advances cursors, and avoids repeating the same history on the next run;
- `partial` persists rejection records and advances both eligible cursors;
- an HTTP, cancellation, parsing, or database failure does not advance either cursor;
- failure of the final completion transaction cannot leave a cursor advanced with a failed run;
- watermarks never move backward;
- source requests use `max_results` values that cannot exceed the remaining budget;
- API responses, `/api/status`, and structured logs expose returned-resource counts, truncation, and stop reason;
- a fresh Alembic upgrade succeeds on SQLite test infrastructure and the supported PostgreSQL deployment path;
- no automated test calls the paid X API.

## Out of scope

This change does not introduce Recent Search, X Activity subscriptions, automatic per-post repair, historical backfill beyond 24 hours, per-List budget customization, billing API reconciliation, or a new user interface for rejection inspection. Those can be added later without blocking the cost-control objective.
