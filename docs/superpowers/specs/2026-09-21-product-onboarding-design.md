# Product Onboarding Design

## Goal

Add a product-native onboarding flow that lets a new single-user installation connect X, add an X List, confirm the global Simplified Chinese summary language, run the first synchronization, and understand what to do next without reading deployment documentation or calling APIs manually.

The same interface remains available after onboarding as the product's settings and List-management entry point.

## Success criteria

- A new installation with no X credential or Lists automatically enters a dedicated onboarding flow from the home page.
- The user can connect X through the existing OAuth 2.0 PKCE flow and return to onboarding with a clear success or failure state.
- The user can add a List by pasting either its numeric ID or a supported X/Twitter List URL.
- The global summary language is persisted as Simplified Chinese (`zh-CN`) and is applied to all future automatic and manual summary generation.
- The user can start the first List synchronization and see a safe, comprehensible result or retryable error.
- A configured installation retains a visible “Settings & Lists” entry without forcing the user through completed steps again.

## Scope

### Included

- A dedicated `/onboarding` frontend route with four steps and a completion state.
- Derived onboarding completion based on durable backend state.
- Safe X connection status reporting.
- Global summary-language persistence and enforcement.
- List input normalization for numeric IDs and supported URLs.
- List creation and first-sync controls using existing backend capabilities.
- Home-page routing and settings entry behavior.
- Backend, frontend, migration, and end-to-end test coverage.

### Excluded

- Fetching List metadata from X to infer a display name.
- Per-List languages or multiple summary-language choices.
- Rewriting historical summaries when the language preference is saved.
- A background job progress transport such as WebSockets or server-sent events.
- Full List editing and deletion in the first onboarding iteration.

## Architecture

### Durable state and completion

Onboarding does not store an independent `completed` flag. Completion is derived from four facts:

1. the server has all required X OAuth configuration;
2. an X OAuth credential exists;
3. at least one enabled X List exists;
4. the global summary language has been saved as `zh-CN`.

This avoids presenting onboarding as complete when an underlying required resource has been removed. An aggregate onboarding status endpoint returns these booleans plus safe synchronization status. It never returns OAuth tokens, ciphertext, provider responses, or other secrets.

A new singleton application-settings record stores the global summary language. Both the backend web process and scheduler read the preference from the database when starting summary work, so deployments with separate processes cannot diverge through in-memory state.

### Summary-language propagation

The summary provider contract receives the target language for each summary request. Automatic pipeline summaries and manual regeneration both resolve the global preference before calling the provider. The OpenAI provider explicitly instructs the model to return Simplified Chinese and still validates the structured response. Deterministic/test providers accept the same contract and report the expected language.

Existing summary and digest snapshots remain immutable. Changing or reconfirming the preference affects only future generations.

### Existing resources

OAuth credentials, Lists, synchronization runs, and pipeline execution continue to use their existing models and services. Onboarding orchestrates these capabilities instead of duplicating their data or business logic.

## Backend interfaces

### Onboarding status

Add a protected read endpoint returning:

- whether X OAuth is configured on the server;
- whether an X credential is stored;
- whether an enabled List exists;
- the current global summary language, or `null` before it is saved;
- derived completion;
- the latest safe synchronization summary already exposed by the observability layer.

Credential presence is checked without decrypting or returning token material. A missing server OAuth configuration is distinct from an unconnected account so the UI can explain an operator configuration problem rather than asking the user to retry authorization indefinitely.

### Global settings

Add protected read and update endpoints for application settings. The update schema is strict and currently accepts only `summary_language: "zh-CN"`. The write is an idempotent singleton upsert.

### Lists and synchronization

The frontend continues to use the existing List creation and manual synchronization endpoints. No second onboarding-specific creation or synchronization path is introduced.

## Frontend flow

### Routing

The home page loads onboarding status alongside its existing data. If onboarding is incomplete, it replaces the reading view with navigation to `/onboarding`; the onboarding route is the canonical first-use surface. A completed home page includes a persistent “Settings & Lists” link to the same route.

The setup route restores progress from backend state after every load. Completed steps remain navigable, and an already configured user sees a settings-oriented summary rather than being forced through the wizard again.

### Step 1: Connect X

The step explains that the application requests read access required for Lists. It shows one of four states: server configuration missing, ready to connect, connected, or authorization failed. The primary action starts the existing PKCE route. Reconnection is available after setup.

OAuth callbacks return to `/onboarding` with a fixed `x_auth` result value. Success and failure redirects never include the provider authorization code, state, verifier, token, or upstream error body.

### Step 2: Add a List

The form contains:

- “List URL or ID”;
- “Display name”.

It accepts a 1–19 digit ID or an HTTPS URL whose hostname is exactly `x.com` or `twitter.com` and whose path is exactly `/i/lists/{1-19 digit ID}`, allowing only a harmless trailing slash. Credentials, alternate schemes, lookalike domains, extra path segments, and invalid IDs are rejected. The normalized numeric ID is submitted to the existing List API.

After creation, the interface shows the current Lists. A duplicate response preserves the existing List and tells the user it has already been added.

### Step 3: Summary language

The step shows “Simplified Chinese” as the global language and saves `zh-CN`. It explains that the preference applies to future generated and regenerated summaries, not historical snapshots.

### Step 4: First synchronization

The user selects an enabled List and starts the existing synchronization operation. The submit control remains pending while the request runs. The result shows the overall status and safe stage counts, including pages fetched, posts created, rejected items, and processing outcomes.

Succeeded and partial results can advance to completion. A failed result remains on the synchronization step, provides a retry action, and maps safe error codes to user-facing messages. It does not render raw upstream errors.

### Completion

The completion state confirms setup and offers “View digests” and “Browse posts.” If no digest exists yet, the digest action returns home with an explanation that digests are generated on schedule, rather than implying data loss or failure.

## Accessibility and responsive behavior

- The step indicator exposes the current step and completed state semantically, not through color alone.
- Pending actions disable duplicate submission and retain a visible label.
- Informational state changes use `role="status"`; actionable failures use `role="alert"`.
- Labels, validation messages, and focus placement identify the field or action that needs correction.
- The wizard and settings summary collapse to a single column on narrow screens.

## Error handling

- OAuth-not-configured and OAuth-not-connected states are distinct.
- List parsing fails locally before any API request and uses the same validation constraints as the backend List schema.
- A duplicate List is non-destructive and recoverable.
- Settings validation rejects unsupported language values.
- Synchronization failures retain the configured OAuth, List, and language state.
- Backend errors are mapped to fixed safe copy. The frontend never displays raw provider bodies or secrets.
- Reloading or navigating away during an in-flight request does not write stale client state; the next load re-derives progress from the backend.

## Testing strategy

Implementation follows test-driven development.

### Backend tests

- Migration and model tests for the singleton application setting.
- Onboarding status tests for every combination of credential, enabled List, and saved language.
- Verification that OAuth status is safe and distinguishes unavailable configuration from no stored credential.
- Strict settings validation and idempotent updates.
- Automatic and manual summary generation tests proving `zh-CN` reaches the provider.
- Existing List and synchronization endpoint regression coverage.

### Frontend tests

- Unit-level tests for valid numeric IDs, valid supported URLs, and rejected URL/domain/path variants where the current frontend test setup permits direct module testing.
- Playwright flows for first-use routing, OAuth return states, List creation from ID and URL, language confirmation, initial sync success, initial sync failure and retry, completion, and reopening settings after setup.
- Assertions for accessible statuses and the absence of secret callback inputs in redirects or rendered content.

### Verification

- Run the complete backend test suite.
- Run the production Next.js build.
- Run the complete frontend test command, including client-asset leak checks and Playwright tests.
- Run migration upgrade tests against a clean database.

## Rollout and compatibility

The migration adds the settings table without modifying existing OAuth, List, summary, or digest records. Existing installations without a settings record are considered incomplete only for the language step; their credential and Lists remain recognized. Saving Simplified Chinese completes that requirement without regenerating historical content.

No external service, new runtime dependency, or new infrastructure component is introduced.
