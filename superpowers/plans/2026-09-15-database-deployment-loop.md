# Database Deployment Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a one-command local Docker Compose deployment in which PostgreSQL becomes healthy, Alembic migrates an empty database, application services start only after migration succeeds, and data survives an ordinary stop/start cycle.

**Architecture:** Add a PostgreSQL 17 service with a named volume and an explicit health check. A one-shot `migrate` service reuses the backend image and gates both the API and scheduler through `service_completed_successfully`; Alembic reads `DATABASE_URL` at runtime while fast unit tests retain SQLite.

**Tech Stack:** Docker Compose, PostgreSQL 17, Python 3.12, SQLAlchemy 2, Alembic 1.x, Psycopg 3, FastAPI, pytest, Ruff, MyPy

**Spec:** `superpowers/specs/2026-09-15-database-deployment-loop-design.md`

## Global Constraints

- Scope is local Docker Compose only; managed databases, TLS, external backups, secret managers, monitoring, and high availability are excluded.
- Use one migration executor. Neither `backend` nor `scheduler` may run migrations themselves.
- `backend`, `scheduler`, and `migrate` must receive the same PostgreSQL `DATABASE_URL` and must never silently fall back to SQLite in Compose.
- Existing unit tests remain SQLite-based.
- Ordinary `docker compose down` and restart operations must preserve data; only an explicit `docker compose down -v` removes the named volume.
- Do not import or modify the existing `backend/x_digest.db`.
- Integration verification must use a unique Compose project name so cleanup cannot target a normal development deployment.

## File Structure

- `backend/tests/test_migrations.py`: verifies that Alembic honors the runtime `DATABASE_URL` while retaining the fresh-SQLite migration test.
- `backend/alembic/env.py`: selects the environment-provided database URL before configuring online or offline migrations.
- `backend/pyproject.toml` and `backend/uv.lock`: add and lock the Psycopg 3 binary distribution used by the backend image.
- `backend/Dockerfile`: includes Alembic configuration and revision files in the reusable runtime image.
- `docker-compose.yml`: owns PostgreSQL, migration gating, service health checks, restart behavior, and persistent volume wiring.
- `.env.example`: provides a complete, non-secret local Compose configuration.
- `backend/.env.example`: retains standalone SQLite defaults and documents the Compose/PostgreSQL distinction.
- `README.md`: documents startup, diagnosis, health verification, schema inspection, persistence verification, shutdown, and explicit data deletion.

---

### Task 1: Runtime Alembic URL and PostgreSQL Driver

**Files:**
- Modify: `backend/tests/test_migrations.py`
- Modify: `backend/alembic/env.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`

**Interfaces:**
- Consumes: `DATABASE_URL`, an optional SQLAlchemy URL supplied through the process environment.
- Produces: Alembic migration commands that prefer `DATABASE_URL` over `alembic.ini` and an installed `psycopg` driver for `postgresql+psycopg://` URLs.

- [ ] **Step 1: Write a failing migration test for the environment URL**

Append this test to `backend/tests/test_migrations.py`:

```python
def test_migration_prefers_database_url_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_path = tmp_path / "configured.sqlite3"
    environment_path = tmp_path / "environment.sqlite3"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{configured_path}")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{environment_path}")

    command.upgrade(config, "head")

    assert environment_path.exists()
    tables = inspect(
        create_engine(f"sqlite+pysqlite:///{environment_path}")
    ).get_table_names()
    assert "alembic_version" in tables
    assert not configured_path.exists()
```

Add `import pytest` beside the existing imports.

- [ ] **Step 2: Run the focused test and verify the current Alembic configuration fails it**

Run from `backend/`:

```bash
uv run pytest tests/test_migrations.py::test_migration_prefers_database_url_environment -v
```

Expected: FAIL because the configured SQLite file is created and the environment-selected file is absent.

- [ ] **Step 3: Make Alembic prefer the runtime URL**

Add `import os` to `backend/alembic/env.py`, then immediately after `config = context.config` add:

```python
database_url = os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
```

The percent escaping is required because Alembic stores the option through `ConfigParser`; `get_main_option()` returns the original URL to SQLAlchemy.

- [ ] **Step 4: Add and lock the PostgreSQL driver**

Add this entry to the main dependency list in `backend/pyproject.toml`:

```toml
  "psycopg[binary]>=3.2,<4",
```

Then run from `backend/`:

```bash
uv lock
```

Expected: `backend/uv.lock` records `psycopg`, `psycopg-binary`, and the updated `x-digest` dependency set.

- [ ] **Step 5: Run migration tests and static checks**

Run from `backend/`:

```bash
uv run pytest tests/test_migrations.py -v
uv run ruff check alembic/env.py tests/test_migrations.py
uv run mypy src
```

Expected: all commands pass.

- [ ] **Step 6: Commit the runtime database support**

```bash
git add backend/alembic/env.py backend/tests/test_migrations.py backend/pyproject.toml backend/uv.lock
git commit -m "feat: support PostgreSQL migration runtime"
```

---

### Task 2: Compose PostgreSQL and Migration Gate

**Files:**
- Create: `.env.example`
- Modify: `backend/.env.example`
- Modify: `backend/Dockerfile`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `INTERNAL_API_TOKEN`, operator credentials, and OAuth configuration from the root `.env` file.
- Produces: `postgres` health status, one-shot `migrate` completion status, healthy `backend` and `frontend` services, and persistent volume `x_digest_postgres_data`.

- [ ] **Step 1: Record the pre-change Compose failure**

Run from the repository root:

```bash
docker compose --env-file .env.example config
```

Expected: FAIL because the root `.env.example` does not yet exist. This establishes the configuration test before implementation.

- [ ] **Step 2: Create the complete local environment template**

Create `.env.example` with URL-safe local-only values so the copied file works without URL encoding:

```dotenv
POSTGRES_DB=x_digest
POSTGRES_USER=x_digest
POSTGRES_PASSWORD=local-x-digest-password
INTERNAL_API_TOKEN=local-internal-api-token-at-least-32-bytes
OPERATOR_USERNAME=operator
OPERATOR_PASSWORD=local-operator-password
X_CLIENT_ID=replace-with-your-x-oauth-client-id
X_OAUTH_REDIRECT_URI=http://localhost:3000/api/x/callback
X_OAUTH_STATE_SECRET=local-x-oauth-state-secret-at-least-32-bytes
X_TOKEN_ENCRYPTION_KEY=replace-with-a-valid-fernet-key
FRONTEND_PORT=3000
```

Add comments explaining that these are development placeholders, the database password must be URL-encoded if it contains URL-reserved characters, and real provider credentials are only needed for OAuth operations.

- [ ] **Step 3: Make the backend image migration-capable**

In `backend/Dockerfile`, copy the Alembic runtime files alongside the package source:

```dockerfile
COPY alembic.ini ./
COPY alembic ./alembic
```

Keep installation and the existing API command unchanged; Compose overrides the command for `migrate` and `scheduler`.

- [ ] **Step 4: Replace shared SQLite with PostgreSQL and explicit service gates**

Update `docker-compose.yml` with these semantics:

```yaml
services:
  postgres:
    image: postgres:17-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB:?Set POSTGRES_DB in .env}
      POSTGRES_USER: ${POSTGRES_USER:?Set POSTGRES_USER in .env}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"]
      interval: 5s
      timeout: 5s
      retries: 12
    volumes:
      - x_digest_postgres_data:/var/lib/postgresql/data

  migrate:
    image: x-digest
    build:
      context: ./backend
    command: ["alembic", "upgrade", "head"]
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
    restart: "no"
```

Apply these additional rules to the existing services:

- `backend` and `scheduler` use the same PostgreSQL `DATABASE_URL` as `migrate` and no longer mount the SQLite volume.
- Both depend on `migrate` with `condition: service_completed_successfully`.
- `backend` has a health check that requests `http://localhost:8000/health/ready` using Python's standard library.
- `frontend` depends on `backend` with `condition: service_healthy`, has a Node-based health check for `http://localhost:3000/health`, and keeps its loopback-only host port.
- Long-running services use `restart: unless-stopped`; `migrate` does not restart.
- Replace `x_digest_data` with `x_digest_postgres_data` in the top-level `volumes` block.
- Inject `X_TOKEN_ENCRYPTION_KEY` into both `backend` and `scheduler`, alongside their existing application settings.

- [ ] **Step 5: Clarify standalone backend configuration**

Keep the SQLite `DATABASE_URL` in `backend/.env.example` and add:

```dotenv
# Docker Compose overrides this with PostgreSQL from the repository-root .env file.
# PostgreSQL example: postgresql+psycopg://x_digest:password@localhost:5432/x_digest
```

- [ ] **Step 6: Validate the rendered Compose model**

Run from the repository root:

```bash
docker compose --env-file .env.example config --quiet
docker compose --env-file .env.example config --services
```

Expected: exit 0, and services include `postgres`, `migrate`, `backend`, `scheduler`, and `frontend`.

Inspect the rendered configuration:

```bash
docker compose --env-file .env.example config
```

Expected: all three Python services use `postgresql+psycopg://...@postgres:5432/x_digest`; backend and scheduler depend on successful migration; no service references `/data/x_digest.db` or `x_digest_data`.

- [ ] **Step 7: Commit the Compose topology**

```bash
git add .env.example backend/.env.example backend/Dockerfile docker-compose.yml
git commit -m "feat: deploy PostgreSQL with migration gate"
```

---

### Task 3: Deployment and Recovery Runbook

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: the Compose services and root `.env.example` from Task 2.
- Produces: operator commands for initial deployment, status inspection, health checks, schema checks, persistence checks, safe shutdown, and explicit destructive cleanup.

- [ ] **Step 1: Write the deployment runbook**

Create `README.md` with a short project introduction and a `Local Docker deployment` section containing these exact workflows:

```bash
cp .env.example .env
docker compose config --quiet
docker compose up --build -d --wait
docker compose ps --all
```

Document that `migrate` should show `Exited (0)` and long-running services should be healthy/running. Add log commands:

```bash
docker compose logs migrate
docker compose logs postgres backend scheduler frontend
```

Add health commands:

```bash
curl --fail http://localhost:${FRONTEND_PORT:-3000}/health
docker compose exec -T backend python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health/ready').read().decode())"
```

Add schema inspection commands:

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "TABLE alembic_version;"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"
```

Explain that shell variables in these two commands require loading `.env` first, for example `set -a; . ./.env; set +a`, or replacing them with the configured literal values.

- [ ] **Step 2: Document a reversible persistence check**

Use a dedicated marker row in `x_lists`:

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "INSERT INTO x_lists (id, platform_list_id, name, sync_interval_minutes, enabled, created_at, updated_at) VALUES ('00000000-0000-0000-0000-000000000001', 'deployment-check', 'Deployment check', 60, false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) ON CONFLICT (platform_list_id) DO UPDATE SET name = EXCLUDED.name;"
docker compose down
docker compose up -d --wait
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT name FROM x_lists WHERE platform_list_id = 'deployment-check';"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DELETE FROM x_lists WHERE platform_list_id = 'deployment-check';"
```

Expected query output: `Deployment check` after the stop/start cycle.

- [ ] **Step 3: Separate safe shutdown from destructive cleanup**

Document `docker compose down` as the normal stop command. Put `docker compose down -v` in a clearly labeled “Delete all local database data” subsection stating that it irreversibly removes the PostgreSQL named volume and is never part of ordinary shutdown.

Include failure diagnosis:

- PostgreSQL unhealthy: inspect `docker compose logs postgres`.
- Migration exited nonzero: inspect `docker compose logs migrate`; application services intentionally remain blocked.
- Backend returns `database_unavailable`: verify PostgreSQL health and the rendered `DATABASE_URL` without printing real secrets in shared logs.

- [ ] **Step 4: Verify every documented command matches the Compose service names**

Run:

```bash
rg -n "postgres|migrate|backend|scheduler|frontend|health/ready|alembic_version|down -v" README.md
docker compose --env-file .env.example config --services
```

Expected: each referenced service appears in the rendered service list, and all required operational topics are present.

- [ ] **Step 5: Commit the runbook**

```bash
git add README.md
git commit -m "docs: add local database deployment runbook"
```

---

### Task 4: Isolated End-to-End Deployment Verification

**Files:**
- Modify if a defect is found: only files introduced or changed by Tasks 1-3

**Interfaces:**
- Consumes: root `.env.example`, the complete Compose topology, Alembic revisions through `0002_oauth_credentials`, and health endpoints.
- Produces: evidence that an empty PostgreSQL volume migrates, services become ready, and a marker row persists across `down`/`up` without volume deletion.

- [ ] **Step 1: Run the complete backend quality suite**

Run from `backend/`:

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

Expected: all commands pass.

- [ ] **Step 2: Start an isolated deployment from an empty volume**

Use the fixed, task-specific project name `x-digest-deployment-check`; verify no project with that name is running before proceeding:

```bash
docker compose -p x-digest-deployment-check --env-file .env.example ps --all
```

If it reports existing containers, stop and inspect them rather than deleting them. Otherwise run:

```bash
env FRONTEND_PORT=33000 docker compose -p x-digest-deployment-check --env-file .env.example up --build -d --wait
env FRONTEND_PORT=33000 docker compose -p x-digest-deployment-check --env-file .env.example ps --all
```

Expected: `postgres`, `backend`, and `frontend` are healthy; `scheduler` is running; `migrate` exited with status 0.

- [ ] **Step 3: Verify migration version, schema, and health**

Run:

```bash
docker compose -p x-digest-deployment-check --env-file .env.example exec -T postgres psql -U x_digest -d x_digest -tAc "SELECT version_num FROM alembic_version;"
docker compose -p x-digest-deployment-check --env-file .env.example exec -T postgres psql -U x_digest -d x_digest -tAc "SELECT to_regclass('public.x_lists'), to_regclass('public.oauth_credentials');"
curl --fail http://localhost:33000/health
docker compose -p x-digest-deployment-check --env-file .env.example exec -T backend python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health/ready').read().decode())"
```

Expected: version `0002_oauth_credentials`, both table names are returned, and both health requests return `{"status":"ok"}`.

- [ ] **Step 4: Verify readiness failure and recovery during a database outage**

Stop only PostgreSQL, then query backend readiness from inside the still-running backend container:

```bash
docker compose -p x-digest-deployment-check --env-file .env.example stop postgres
docker compose -p x-digest-deployment-check --env-file .env.example exec -T backend python -c "import http.client; connection = http.client.HTTPConnection('localhost', 8000); connection.request('GET', '/health/ready'); response = connection.getresponse(); print(response.status, response.read().decode())"
docker compose -p x-digest-deployment-check --env-file .env.example start postgres
docker compose -p x-digest-deployment-check --env-file .env.example exec -T postgres pg_isready -U x_digest -d x_digest
docker compose -p x-digest-deployment-check --env-file .env.example exec -T backend python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health/ready').read().decode())"
```

Expected: the outage request prints status `503` with code `database_unavailable`; after PostgreSQL becomes ready, the same backend process returns `{"status":"ok"}` without an application restart. If `pg_isready` runs before recovery completes, repeat only that read-only readiness command until it succeeds.

- [ ] **Step 5: Write the marker and stop without deleting the volume**

```bash
docker compose -p x-digest-deployment-check --env-file .env.example exec -T postgres psql -U x_digest -d x_digest -c "INSERT INTO x_lists (id, platform_list_id, name, sync_interval_minutes, enabled, created_at, updated_at) VALUES ('00000000-0000-0000-0000-000000000001', 'deployment-check', 'Deployment check', 60, false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);"
docker compose -p x-digest-deployment-check --env-file .env.example down
```

Expected: the marker insert succeeds and Compose removes containers/networks without removing the named volume.

- [ ] **Step 6: Restart and prove persistence**

```bash
env FRONTEND_PORT=33000 docker compose -p x-digest-deployment-check --env-file .env.example up -d --wait
docker compose -p x-digest-deployment-check --env-file .env.example exec -T postgres psql -U x_digest -d x_digest -tAc "SELECT name FROM x_lists WHERE platform_list_id = 'deployment-check';"
```

Expected: the query prints `Deployment check`, and the migration service again exits 0 because upgrades are idempotent.

- [ ] **Step 7: Remove only the isolated verification deployment**

After confirming the exact project name in `docker compose ... ps`, delete the marker and remove this verification project's containers and volume:

```bash
docker compose -p x-digest-deployment-check --env-file .env.example exec -T postgres psql -U x_digest -d x_digest -c "DELETE FROM x_lists WHERE platform_list_id = 'deployment-check';"
docker compose -p x-digest-deployment-check --env-file .env.example down -v
```

Expected: only resources labeled for the `x-digest-deployment-check` project are removed. Do not run `down -v` without the explicit `-p x-digest-deployment-check` scope.

- [ ] **Step 8: Review final changes and commit any verification fixes**

```bash
git status --short
git diff --check
git log --oneline -4
```

Expected: no uncommitted changes. If verification exposed a defect, add a focused regression test, make the minimum fix, rerun the relevant checks plus Steps 1-5, then commit only those files with a message describing the defect.
