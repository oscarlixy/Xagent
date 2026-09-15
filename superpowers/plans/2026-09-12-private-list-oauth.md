# Private X List OAuth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or executing-plans task-by-task.

**Goal:** Let the single operator authorize private X List reads through OAuth 2.0 PKCE, with encrypted backend token storage and refresh.

**Architecture:** Next.js owns the browser redirect/callback and short-lived signed PKCE state; FastAPI owns token exchange, encryption, refresh, and official X calls. The browser never sees an X access/refresh token or backend credentials.

**Tech Stack:** Next.js 15, Web Crypto, FastAPI, httpx, cryptography/Fernet, SQLAlchemy, Alembic, pytest, Playwright.

**Spec:** `superpowers/specs/2026-09-12-private-list-oauth-design.md`

## Global constraints

- OAuth scopes are exactly `tweet.read users.read list.read offline.access`.
- All tests mock the X authorization/token/API endpoints; no test calls X.
- State Cookie is HMAC-signed, HttpOnly, Secure in production, SameSite=Lax, path `/api/x`, and expires within 10 minutes.
- Token encryption key and every access/refresh token/code/verifier stay out of logs, responses, frontend assets, and version control.
- Persist one record for provider `x`; refresh at most once for one expired credential transaction.
- The private backend remains unreachable from the host; only the Basic-Auth-protected frontend is host-published, except state-protected OAuth callback.

### Task 1: Add encrypted OAuth credential persistence

**Files:** `backend/pyproject.toml`, `backend/src/x_digest/config.py`, `backend/src/x_digest/models/oauth.py`, `backend/src/x_digest/models/__init__.py`, `backend/alembic/versions/0002_oauth_credentials.py`, `backend/src/x_digest/services/oauth_tokens.py`, `backend/tests/services/test_oauth_tokens.py`.

- [ ] Write failing tests for Fernet round trip, invalid key rejection, encrypted database fields, one `provider` row, and expiry calculation.
- [ ] Run targeted pytest and observe missing module failure.
- [ ] Add `cryptography`, settings, model/migration, and a token vault that only accepts validated token payloads.
- [ ] Run targeted/full backend tests, Ruff, mypy; commit `feat: store encrypted x oauth credentials`.

### Task 2: Exchange and refresh tokens in the private backend

**Files:** `backend/src/x_digest/services/oauth_client.py`, `backend/src/x_digest/api/x_oauth.py`, `backend/src/x_digest/main.py`, `backend/tests/services/test_oauth_client.py`, `backend/tests/api/test_x_oauth.py`.

- [ ] Write failing httpx-transport tests for authorization-code exchange, refresh, token payload validation, safe failure codes, and no token in API JSON.
- [ ] Implement injected async HTTP OAuth client plus protected `POST /api/x/oauth/callback`; use token vault and 60-second refresh margin.
- [ ] Run targeted/full checks; commit `feat: exchange and refresh x oauth tokens`.

### Task 3: Add BFF PKCE initiation and callback

**Files:** `frontend/src/app/api/x/authorize/route.ts`, `frontend/src/app/api/x/callback/route.ts`, `frontend/src/lib/oauth-state.ts`, `frontend/src/middleware.ts`, `frontend/.env.example`, `frontend/tests/reading-flow.spec.ts`, `frontend/tests/client-assets.test.mjs`.

- [ ] Write failing Playwright/unit tests for challenge method, signed state, missing/tampered/expired state, callback proxy, cookie deletion, fixed redirect, and no secret asset output.
- [ ] Implement HMAC state, PKCE S256, authorization redirect, and callback forwarding to backend with server-only token.
- [ ] Keep exactly `/api/x/callback` Basic-Auth-exempt; test lookalikes still challenge.
- [ ] Run npm test/build; commit `feat: add x oauth pkce callback`.

### Task 4: Implement the official OAuth-backed X List source and preflight

**Files:** `backend/src/x_digest/sources/errors.py`, `backend/src/x_digest/sources/x_api.py`, `backend/src/x_digest/sources/__init__.py`, `backend/scripts/check_x_access.py`, `backend/tests/sources/test_x_api.py`, `backend/tests/e2e/test_private_list_oauth_flow.py`.

- [ ] Write fixture/transport tests for OAuth authorization header, exact List endpoint fields, pagination, partial errors, 429/5xx retry, and refresh-before-read.
- [ ] Implement source adapter with an injected token service; preserve valid page data when item errors exist; redact provider body/error details.
- [ ] Make preflight opt-in, one item maximum, output status/count/request ID only; test it never prints a secret.
- [ ] Run backend/frontend checks and Compose config; commit `feat: sync private x lists via oauth`.
