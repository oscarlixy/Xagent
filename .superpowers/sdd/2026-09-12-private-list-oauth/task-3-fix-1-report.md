# Task 3 review fix 1

Implemented the three review fixes:

- Replaced direct OAuth-state string equality with a UTF-8 byte, full-length constant-time comparison.
- Centralized OAuth callback configuration for both routes. `X_OAUTH_REDIRECT_URI` must be an HTTP(S), same-request-origin `/api/x/callback` URL without credentials, query, or fragment. Invalid backend URLs and internal tokens shorter than 32 UTF-8 bytes fail before authorization redirects or state-cookie creation.
- Kept callback error redirects fixed and cookie behavior unchanged. Request-origin validation uses the request Host plus protocol: Next's `nextUrl.origin` was observed to normalize to `localhost` under `next start`, even when the received Host was `127.0.0.1`.

TDD evidence:

- RED: `npx playwright test tests/oauth-hardening.spec.ts` — 9 expected failures: missing comparison helper and authorize returned 307 for absent/invalid callback prerequisites and invalid redirect-URI forms.
- RED: added Host-origin regression; targeted suite then failed with `expected 503, received 307`.
- GREEN: `npx playwright test tests/oauth-hardening.spec.ts` — 10 passed.

Verification:

- `npm test` — Next production build succeeded, client asset scan passed, 35 Playwright tests passed.
- `docker compose config --quiet` with test-only environment values — passed.
