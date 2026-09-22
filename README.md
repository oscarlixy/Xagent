# X Digest

X Digest collects and manages X list content. This repository includes a local Docker Compose deployment with PostgreSQL, a one-time database migration gate, backend and scheduler services, and the frontend.

## Local Docker deployment

Create a local environment file, validate the rendered configuration, then build and start the stack:

```bash
cp .env.example .env
docker compose config --quiet
docker compose up --build -d --wait
docker compose ps --all
```

Open the local UI at `http://127.0.0.1:${FRONTEND_PORT:-3000}`. For X OAuth, register
`http://127.0.0.1:3000/api/x/callback` as the exact callback URI and use `127.0.0.1` rather than
`localhost` when visiting the app, because the request origin must match the configured callback.
For an X **Web App** or **Automated App/Bot**, also set `X_CLIENT_SECRET` in the repository-root
`.env`; these are confidential clients and X requires HTTP Basic authentication at the token
endpoint. Leave it empty only for a **Native App** or **Single Page App**, which is a public client.

`POSTGRES_PASSWORD` is used both by PostgreSQL and in `DATABASE_URL`. Use only URL-safe (unreserved) characters: letters, digits, `-`, `.`, `_`, and `~`; do not URL-encode it.

The `migrate` service should show `Exited (0)`. The long-running `postgres`, `backend`, `scheduler`, and `frontend` services should be healthy or running.

Inspect startup and runtime logs with:

```bash
docker compose logs migrate
docker compose logs postgres backend scheduler frontend
```

Check the frontend and backend readiness endpoints:

```bash
curl --fail http://127.0.0.1:${FRONTEND_PORT:-3000}/health
docker compose exec -T backend python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health/ready').read().decode())"
```

## X access and incremental synchronization

The source uses X's current List posts contract (`post.fields` and `referenced_posts`) while still
accepting the legacy response field during transition. Before enabling scheduled collection, run an
explicit, redacted one-item probe from the backend environment:

```bash
cd backend
uv run python scripts/check_x_access.py --list-id YOUR_NUMERIC_LIST_ID --confirm
```

This real request can require a paid X API tier and appropriate List permissions. CI never calls X.
After a successful run, subsequent runs stop once a complete page is older than the stored watermark
minus `X_INGESTION_OVERLAP_MINUTES` (default 10). Page and post caps remain hard safety limits.

Link fetches run outside database transactions. `LINK_MAX_ITEMS_PER_RUN`, `LINK_MAX_ATTEMPTS`, and
`LINK_RETRY_BASE_SECONDS` bound each run and its exponential retries. Summary provider calls likewise
run outside database transactions, use bounded retries, and cap combined post/link input at 120,000
characters.

## Real LLM summaries

The test-safe default is `LLM_PROVIDER=deterministic`. For Docker Compose, put the Gemini API key
in the repository-root `.env`, then configure OpenAI as primary and Gemini as its quota fallback:

```dotenv
# .env (injected only into backend and scheduler)
GEMINI_API_KEY=your-gemini-api-key

# backend/.env
LLM_PROVIDER=openai
LLM_FALLBACK_PROVIDER=gemini
LLM_API_KEY=your-openai-api-key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-5.6-luna
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_THINKING_LEVEL=low
```

OpenAI remains the primary summary provider. It performs its bounded retries first; only an
exhausted HTTP 429 response switches that item to Gemini. Authentication failures, malformed
requests, invalid structured output, network failures, and ordinary 5xx responses do not trigger
the fallback. Gemini uses the stable Interactions API at `/v1/interactions`, disables server-side
storage, and requests JSON matching the summary schema.

The Gemini key is not passed to the frontend container. Use a project-scoped key and never commit
either `.env` file. For direct backend runs outside Docker Compose, set `GEMINI_API_KEY` in the
shell or in `backend/.env`.

## Event-centered digests

Completed windows are synthesized into topics and concrete events instead of a flat collection of
unrelated summaries. Each event explains “发生了什么” and “为什么值得关注”, and its source links are
limited to the posts assigned to that event. Low-information promotion, conference advertising,
and podcast previews can be excluded from the digest without deleting their source posts. Every
explicit regeneration creates a new immutable digest version, so earlier content and source
assignments remain available for audit and comparison.

## Optional notifications

Notifications are disabled unless a complete channel configuration is present in `backend/.env`.
Set `NOTIFICATION_FRONTEND_URL` plus either `SMTP_HOST`, `SMTP_FROM`, and `SMTP_TO` (with optional
SMTP credentials), or `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Delivery is recorded in the
database, retried with a bounded backoff, and only starts after the Digest transaction commits.
Provider credentials and response bodies are not logged.

## Local quality checks

```bash
cd backend
uv run ruff check .
uv run mypy src
uv run pytest
cd ../frontend
npm test
```

GitHub Actions runs these checks, a fresh Alembic upgrade, the frontend build, and Docker Compose
configuration validation without real provider credentials.

Inspect the applied migration revision and database tables:

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "TABLE alembic_version;"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"
```

The schema-inspection commands use shell variables. Load `.env` first (for example, `set -a; . ./.env; set +a`) or replace `$POSTGRES_USER` and `$POSTGRES_DB` with their configured literal values.

### Reversible persistence check

After loading `.env` as described above, insert a dedicated marker row, stop and restart the stack, confirm the row remains, and remove it:

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "INSERT INTO x_lists (id, platform_list_id, name, sync_interval_minutes, enabled, created_at, updated_at) VALUES ('00000000-0000-0000-0000-000000000001', 'deployment-check', 'Deployment check', 60, false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) ON CONFLICT (platform_list_id) DO UPDATE SET name = EXCLUDED.name;"
docker compose down
docker compose up -d --wait
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT name FROM x_lists WHERE platform_list_id = 'deployment-check';"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DELETE FROM x_lists WHERE platform_list_id = 'deployment-check';"
```

The query output should be `Deployment check` after the stop/start cycle. Normal `docker compose down` preserves the PostgreSQL named volume and its data.

### Safe shutdown

Use the normal stop command when you are finished with the local stack:

```bash
docker compose down
```

This shuts down the containers while preserving the PostgreSQL named volume and database data.

### Delete all local database data

```bash
docker compose down -v
```

This irreversibly removes the PostgreSQL named volume and all local database data. It is never part of ordinary shutdown.

### Failure diagnosis

- **PostgreSQL unhealthy:** inspect `docker compose logs postgres`.
- **Migration exited nonzero:** inspect `docker compose logs migrate`; application services intentionally remain blocked until migrations complete successfully.
- **Backend returns `database_unavailable`:** verify PostgreSQL health and the rendered `DATABASE_URL`, without printing real secrets in shared logs.
