import { expect, test } from "@playwright/test";

const oauthStateSecret = "test-x-oauth-state-secret-32-bytes-minimum";
const oauthClientId = "test-x-client-id-must-stay-server-side";
const oauthRedirectUri = "http://127.0.0.1:3100/api/x/callback";

function base64url(value: Uint8Array | string): string {
  const bytes = typeof value === "string" ? Buffer.from(value) : Buffer.from(value);
  return bytes.toString("base64url");
}

async function signedStateCookie(payload: {
  state: string;
  verifier: string;
  expiry: number;
}): Promise<string> {
  const encodedPayload = base64url(JSON.stringify(payload));
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(oauthStateSecret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(encodedPayload),
  );
  return `${encodedPayload}.${base64url(new Uint8Array(signature))}`;
}

function oauthCookie(response: { headersArray(): { name: string; value: string }[] }): string {
  const header = response
    .headersArray()
    .find(({ name }) => name.toLowerCase() === "set-cookie")?.value;
  expect(header).toBeDefined();
  return header!.split(";", 1)[0];
}

function decodedCookiePayload(cookie: string): {
  state: string;
  verifier: string;
  expiry: number;
} {
  const value = cookie.slice(cookie.indexOf("=") + 1);
  const [payload] = value.split(".");
  return JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
}

function expectClearedOAuthCookie(response: {
  headersArray(): { name: string; value: string }[];
}) {
  const header = response
    .headersArray()
    .find(({ name }) => name.toLowerCase() === "set-cookie")?.value;
  expect(header).toContain("x_oauth_state=");
  expect(header?.toLowerCase()).toContain("max-age=0");
  expect(header?.toLowerCase()).toContain("httponly");
  expect(header?.toLowerCase()).toContain("samesite=lax");
  expect(header?.toLowerCase()).toContain("path=/api/x");
}

test("allows anonymous access to the reading interface", async ({ request }) => {
  const response = await request.get("/");

  expect(response.status()).toBe(200);
  expect(response.headers()["www-authenticate"]).toBeUndefined();
});

test("proxies API requests with server credentials and preserves the backend request ID", async ({
  request,
}) => {
  const response = await request.get("/api/digests");

  expect(response.status()).toBe(200);
  expect((await response.json())[0].id).toBe("digest-1");
  expect(response.headers()["x-request-id"]).toBe("mock-request-id");
});

test("forwards only allowlisted BFF request and response headers", async ({ request }) => {
  const response = await request.post("/api/inspect?topic=ai", {
    headers: {
      Accept: "application/vnd.x-digest+json",
      Cookie: "operator_session=must-not-pass",
      "Idempotency-Key": "summary-request-1",
      "X-Untrusted": "must-not-pass",
    },
    data: { visible: true },
  });

  expect(response.status()).toBe(200);
  expect(await response.json()).toMatchObject({
    method: "POST",
    query: "topic=ai",
    headers: {
      authorization: "Bearer test-internal-token-that-must-stay-server-side",
      accept: "application/vnd.x-digest+json",
      "content-type": "application/json",
      "idempotency-key": "summary-request-1",
      cookie: null,
      "x-untrusted": null,
    },
  });
  expect(response.headers()["x-request-id"]).toBe("inspect-request-id");
  expect(response.headers()["x-upstream-secret"]).toBeUndefined();
});

test("rejects methods outside the BFF allowlist", async ({ request }) => {
  const response = await request.put("/api/inspect");
  expect(response.status()).toBe(405);
});

test("leaves the health endpoint public", async ({ request }) => {
  const response = await request.get("/health");
  expect(response.status()).toBe(200);
  expect(await response.json()).toEqual({ status: "ok" });
});

test("starts X authorization with S256 PKCE and a protected state cookie", async ({ request }) => {
  const response = await request.get("/api/x/authorize", {
    maxRedirects: 0,
  });

  expect(response.status()).toBe(307);
  const location = new URL(response.headers().location);
  expect(location.origin + location.pathname).toBe("http://127.0.0.1:9100/mock-x/authorize");
  expect(Object.fromEntries(location.searchParams)).toMatchObject({
    client_id: oauthClientId,
    redirect_uri: oauthRedirectUri,
    response_type: "code",
    code_challenge_method: "S256",
    scope: "tweet.read users.read list.read offline.access",
  });

  const cookie = oauthCookie(response);
  const setCookie = response.headersArray().find(({ name }) => name.toLowerCase() === "set-cookie")!
    .value;
  expect(setCookie.toLowerCase()).toContain("httponly");
  expect(setCookie.toLowerCase()).toContain("samesite=lax");
  expect(setCookie.toLowerCase()).toContain("path=/api/x");
  expect(setCookie.toLowerCase()).toContain("max-age=600");
  expect(setCookie.toLowerCase()).toContain("secure");

  const payload = decodedCookiePayload(cookie);
  expect(Number.isInteger(payload.expiry)).toBe(true);
  expect(payload.expiry).toBeGreaterThan(Math.floor(Date.now() / 1000));
  expect(location.searchParams.get("state")).toBe(payload.state);
  expect(location.searchParams.toString()).not.toContain(payload.verifier);
  const verifierDigest = await crypto.subtle.digest("SHA-256", Buffer.from(payload.verifier));
  expect(location.searchParams.get("code_challenge")).toBe(
    base64url(new Uint8Array(verifierDigest)),
  );
});

test("rejects missing, tampered, and expired OAuth state without leaking inputs", async ({
  request,
}) => {
  const authorization = await request.get("/api/x/authorize", {
    maxRedirects: 0,
  });
  const validCookie = oauthCookie(authorization);
  const validPayload = decodedCookiePayload(validCookie);
  const cookieValue = validCookie.slice(validCookie.indexOf("=") + 1);
  const tamperedCookie = `x_oauth_state=${cookieValue.slice(0, -1)}${cookieValue.endsWith("a") ? "b" : "a"}`;
  const expiredState = "expired-state-must-not-leak";
  const expiredVerifier = "expired-verifier-must-not-leak";
  const expiredCookie = `x_oauth_state=${await signedStateCookie({
    state: expiredState,
    verifier: expiredVerifier,
    expiry: Math.floor(Date.now() / 1000) - 1,
  })}`;

  for (const scenario of [
    { cookie: undefined, state: validPayload.state, code: "missing-cookie-code-must-not-leak" },
    { cookie: tamperedCookie, state: validPayload.state, code: "tampered-code-must-not-leak" },
    { cookie: expiredCookie, state: expiredState, code: "expired-code-must-not-leak" },
  ]) {
    const response = await request.get(
      `/api/x/callback?code=${encodeURIComponent(scenario.code)}&state=${encodeURIComponent(scenario.state)}`,
      {
        headers: { Cookie: scenario.cookie ?? "" },
        maxRedirects: 0,
      },
    );
    expect(response.status()).toBe(307);
    expect(response.headers().location).toBe("http://127.0.0.1:3100/?x_auth=failed");
    expect(response.headers().location).not.toContain(scenario.code);
    expect(response.headers().location).not.toContain(scenario.state);
    expect(response.headers().location).not.toContain(expiredVerifier);
    expectClearedOAuthCookie(response);
  }
});

test("turns an X denial into the fixed denied redirect and clears state", async ({ request }) => {
  const state = "denied-state-must-not-leak";
  const verifier = "denied-verifier-must-not-leak";
  const cookie = await signedStateCookie({
    state,
    verifier,
    expiry: Math.floor(Date.now() / 1000) + 300,
  });
  const response = await request.get(
    `/api/x/callback?error=access_denied&state=${encodeURIComponent(state)}`,
    { headers: { Cookie: `x_oauth_state=${cookie}` }, maxRedirects: 0 },
  );

  expect(response.status()).toBe(307);
  expect(response.headers().location).toBe("http://127.0.0.1:3100/?x_auth=denied");
  expect(response.headers().location).not.toContain(state);
  expect(response.headers().location).not.toContain(verifier);
  expectClearedOAuthCookie(response);
});

test("forwards only the callback code and verifier with server credentials", async ({ request }) => {
  const authorization = await request.get("/api/x/authorize", {
    maxRedirects: 0,
  });
  const cookie = oauthCookie(authorization);
  const payload = decodedCookiePayload(cookie);
  const code = "successful-callback-code-must-not-leak";
  const response = await request.get(
    `/api/x/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(payload.state)}`,
    {
      headers: {
        Cookie: cookie,
        "X-Untrusted": "callback-header-must-not-pass",
      },
      maxRedirects: 0,
    },
  );

  expect(response.status()).toBe(307);
  expect(response.headers().location).toBe("http://127.0.0.1:3100/");
  expectClearedOAuthCookie(response);

  const inspection = await request.get("http://127.0.0.1:9100/oauth-inspect");
  expect(await inspection.json()).toEqual({
    body: { code, verifier: payload.verifier },
    headers: {
      authorization: "Bearer test-internal-token-that-must-stay-server-side",
      content_type: "application/json",
      cookie: null,
      x_untrusted: null,
    },
  });
});

test("redacts callback inputs when the backend exchange fails", async ({ request }) => {
  const state = "failure-state-must-not-leak";
  const verifier = "failure-verifier-must-not-leak";
  const code = "upstream-failure-code-must-not-leak";
  const cookie = await signedStateCookie({
    state,
    verifier,
    expiry: Math.floor(Date.now() / 1000) + 300,
  });
  const response = await request.get(
    `/api/x/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`,
    { headers: { Cookie: `x_oauth_state=${cookie}` }, maxRedirects: 0 },
  );

  expect(response.status()).toBe(307);
  expect(response.headers().location).toBe("http://127.0.0.1:3100/?x_auth=failed");
  expect(response.headers().location).not.toContain(code);
  expect(response.headers().location).not.toContain(state);
  expect(response.headers().location).not.toContain(verifier);
  expectClearedOAuthCookie(response);
});

test("returns safe 503 responses for missing or invalid OAuth configuration", async ({ request }) => {
  for (const port of [3101, 3102]) {
    const authorize = await request.get(`http://127.0.0.1:${port}/api/x/authorize`, {
      maxRedirects: 0,
    });
    expect(authorize.status(), `authorize:${port}`).toBe(503);
    expect(await authorize.json()).toEqual({
      code: "oauth_not_configured",
      message: "X OAuth is not configured",
    });

    const callback = await request.get(
      `http://127.0.0.1:${port}/api/x/callback?code=config-code-must-not-leak&state=config-state-must-not-leak`,
      { maxRedirects: 0 },
    );
    expect(callback.status(), `callback:${port}`).toBe(503);
    const callbackBody = await callback.text();
    expect(callbackBody).not.toContain("config-code-must-not-leak");
    expect(callbackBody).not.toContain("config-state-must-not-leak");
    expectClearedOAuthCookie(callback);
  }
});

test("shows digest list and a safe detail view with original source links", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "X List Digest" })).toBeVisible();
  await page.getByRole("link", { name: "Daily AI Brief" }).click();
  await expect(page.getByRole("heading", { name: "Daily AI Brief" })).toBeVisible();
  await expect(page.getByText("A <script>danger()</script> update, rendered safely.")).toBeVisible();
  await expect(page.locator("script").filter({ hasText: "danger" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "View original on X" })).toHaveAttribute(
    "href",
    "https://x.com/alice/status/19001",
  );
});

test("renders event digests as ordered topics with source-aligned cards", async ({ page }) => {
  await page.goto("/digests/digest-events");

  await expect(page.getByText("本时段主要关注 Agent 稳定性与开发工具。", { exact: true })).toHaveCount(1);
  const topicHeadings = page.locator(".digest-topic > h2");
  await expect(topicHeadings).toHaveText(["Agent 与模型", "开发工具"]);
  await expect(page.getByRole("heading", { name: "长时间 Agent 任务存在语言质量漂移" })).toBeVisible();
  await expect(page.getByText("发生了什么", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("为什么值得关注", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("观点", { exact: true })).toBeVisible();
  await expect(page.getByText("播客预告，无新增信息")).toHaveCount(0);
  await expect(page.locator("[data-event-id='event-agent'] .source-link")).toHaveCount(2);
  await expect(page.locator("[data-event-id='event-agent'] .source-link").nth(0)).toContainText("@alice");
  await expect(page.locator("[data-event-id='event-agent'] .source-link").nth(1)).toContainText("@bob");
  await expect(page.locator("[data-event-id='event-agent']")).toContainText("Alice、Bob · 2 条来源");
  await expect(page.locator("[data-event-id='event-opinion'] .source-link")).toHaveCount(1);
});

test("renders an empty event digest without event cards", async ({ page }) => {
  await page.goto("/digests/digest-empty");

  await expect(page.getByText("该时间段没有可摘要的有效动态。")).toBeVisible();
  await expect(page.locator(".digest-event")).toHaveCount(0);
});

test("keeps a loaded digest trustworthy when live source enrichment fails", async ({ page }) => {
  await page.route("**/api/posts?limit=100", async (route) => {
    await route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"posts unavailable"}' });
  });

  await page.goto("/digests/digest-1");

  await expect(page.getByRole("heading", { name: "Daily AI Brief" })).toBeVisible();
  await expect(page.getByText("A concise, traceable summary.")).toBeVisible();
  await expect(page.locator(".error-message[role=alert]")).toHaveCount(0);
  await expect(page.getByRole("status")).toContainText("Digest loaded successfully");
  await expect(page.getByRole("status")).toContainText("saved digest snapshot below is unchanged");
});

test("shows a digest load error only when the digest request itself fails", async ({ page }) => {
  await page.goto("/digests/error");

  await expect(page.locator(".error-message[role=alert]")).toHaveText("Could not load this digest.");
  await expect(page.getByRole("heading")).toHaveCount(0);
});

test("warns when data is stale and shows the latest sync failure", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("alert", { name: "Data freshness" })).toContainText("Data is stale");
  await expect(page.getByText("Last sync")).toBeVisible();
  await expect(page.locator("time[datetime='2026-09-17T11:00:00Z']")).toBeVisible();
  await expect(page.getByText(/Summary processing failed/i)).toBeVisible();
});

test("does not make an untrusted post source URL clickable", async ({ page }) => {
  await page.goto("/posts?author=unsafe-link");

  await expect(page.getByText("Original launch notes with practical deployment details.")).toBeVisible();
  await expect(page.getByRole("link", { name: "View original on X" })).toHaveCount(0);
});

test("does not make an untrusted digest snapshot URL clickable", async ({ page }) => {
  await page.goto("/digests/digest-unsafe");

  await expect(page.getByRole("heading", { name: "Unsafe Digest" })).toBeVisible();
  await expect(page.getByRole("link", { name: "View original on X" })).toHaveCount(0);
});

test("keeps every post filter in the URL and shows the matching stream", async ({ page }) => {
  await page.goto("/posts");

  await page.getByLabel("List ID").fill("list-1");
  await page.getByLabel("Topic").fill("ai");
  await page.getByLabel("Author").fill("alice");
  await page.getByLabel("From").fill("2026-09-01");
  await page.getByLabel("To", { exact: true }).fill("2026-09-11");
  await page.getByLabel("State").selectOption("saved");
  await page.getByRole("button", { name: "Apply filters" }).click();

  await expect(page).toHaveURL(/list_id=list-1/);
  await expect(page).toHaveURL(/topic=ai/);
  await expect(page).toHaveURL(/author=alice/);
  await expect(page).toHaveURL(/from=2026-09-01/);
  await expect(page).toHaveURL(/to=2026-09-11/);
  await expect(page).toHaveURL(/state=saved/);
  await expect(page.getByText("Original launch notes with practical deployment details.")).toBeVisible();
  await expect(page.getByRole("link", { name: "View original on X" })).toBeVisible();
});

test("includes the whole selected end date in the backend filter", async ({ page }) => {
  await page.goto("/posts?author=date-boundary&to=2026-09-11");

  await expect(page).toHaveURL(/to=2026-09-11/);
  await expect(page.getByText("A post from the final minute of the selected day.")).toBeVisible();
});

test("loads and appends the next post cursor page", async ({ page }) => {
  await page.goto("/posts?author=pagination");

  await expect(page.getByText("First page post")).toBeVisible();
  await page.getByRole("button", { name: "Load more posts" }).click();
  await expect(page.getByText("First page post")).toBeVisible();
  await expect(page.getByText("Second page post")).toBeVisible();
  await expect(page.getByRole("button", { name: "Load more posts" })).toHaveCount(0);
});

test("updates post state optimistically and regenerates its summary", async ({ page }) => {
  await page.goto("/posts");
  const read = page.getByRole("button", { name: "Mark as read" });
  const save = page.getByRole("button", { name: "Save post" });
  const ignore = page.getByRole("button", { name: "Ignore post" });

  await read.click();
  await expect(read).toHaveAttribute("aria-pressed", "true");
  await save.click();
  await expect(save).toHaveAttribute("aria-pressed", "true");
  await ignore.click();
  await expect(ignore).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Regenerate summary" }).click();
  await expect(page.getByText("Regenerated summary for the complete source unit.")).toBeVisible();
});

test("rolls back an optimistic action and announces the failure", async ({ page }) => {
  await page.goto("/posts?author=fail-actions");
  const save = page.getByRole("button", { name: "Save post" });

  await save.click();
  await expect(save).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator(".error-message[role=alert]")).toContainText("Could not update this post");
});

test("regenerates a digest and shows the new immutable version", async ({ page }) => {
  await page.goto("/digests/digest-1");

  await page.getByRole("button", { name: "Regenerate digest" }).click();
  await expect(page).toHaveURL(/\/digests\/digest-2$/);
  await expect(page.getByText("Version 2")).toBeVisible();
  await expect(page.getByText("A newly sourced post for digest version two.")).toBeVisible();
  await expect(page.getByRole("link", { name: "View original on X" })).toHaveAttribute(
    "href",
    "https://twitter.com/bob/status/19002",
  );
});

test("shows loading, empty, and backend error states", async ({ page }) => {
  await page.goto("/posts?author=loading");
  await expect(page.getByText("Loading posts…")).toBeVisible();
  await expect(page.getByText("Original launch notes with practical deployment details.")).toBeVisible();

  await page.goto("/posts?author=empty");
  await expect(page.getByText("No posts match these filters.")).toBeVisible();

  await page.goto("/posts?author=error");
  await expect(page.locator(".error-message[role=alert]")).toContainText("Could not load posts");
});
