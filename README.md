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

The `migrate` service should show `Exited (0)`. The long-running `postgres`, `backend`, `scheduler`, and `frontend` services should be healthy or running.

Inspect startup and runtime logs with:

```bash
docker compose logs migrate
docker compose logs postgres backend scheduler frontend
```

Check the frontend and backend readiness endpoints:

```bash
curl --fail http://localhost:${FRONTEND_PORT:-3000}/health
docker compose exec -T backend python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health/ready').read().decode())"
```

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
