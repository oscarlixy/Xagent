import { expect, test, type Browser } from "@playwright/test";

const operatorHeaders = {
  Authorization: `Basic ${Buffer.from("reader:correct-horse-battery-staple").toString("base64")}`,
};

async function authenticatedPage(browser: Browser) {
  const context = await browser.newContext({
    httpCredentials: { username: "reader", password: "correct-horse-battery-staple" },
  });
  return { context, page: await context.newPage() };
}

test("requires operator Basic Auth for the reading interface", async ({ request }) => {
  const response = await request.get("/");

  expect(response.status()).toBe(401);
  expect(response.headers()["www-authenticate"]).toBe('Basic realm="X Digest"');
});

test("rejects incorrect operator Basic Auth credentials", async ({ browser }) => {
  const context = await browser.newContext({
    httpCredentials: { username: "reader", password: "incorrect-password" },
  });
  const response = await context.request.get("/");

  expect(response.status()).toBe(401);
  expect(response.headers()["www-authenticate"]).toBe('Basic realm="X Digest"');
  await context.close();
});

test("proxies API requests with server credentials and preserves the backend request ID", async ({
  request,
}) => {
  const response = await request.get("/api/digests", { headers: operatorHeaders });

  expect(response.status()).toBe(200);
  expect((await response.json())[0].id).toBe("digest-1");
  expect(response.headers()["x-request-id"]).toBe("mock-request-id");
});

test("forwards only allowlisted BFF request and response headers", async ({ request }) => {
  const response = await request.post("/api/inspect?topic=ai", {
    headers: {
      ...operatorHeaders,
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
  const response = await request.put("/api/inspect", { headers: operatorHeaders });
  expect(response.status()).toBe(405);
});

test("leaves the health endpoint public", async ({ request }) => {
  const response = await request.get("/health");
  expect(response.status()).toBe(200);
  expect(await response.json()).toEqual({ status: "ok" });
});

test("does not exempt paths below the health endpoint from authentication", async ({ request }) => {
  const response = await request.get("/health/private");
  expect(response.status()).toBe(401);
});

test("does not treat static and image lookalike prefixes as public assets", async ({ request }) => {
  for (const path of ["/_next/static-evil/file.js", "/_next/image-proxy/file.png"]) {
    const response = await request.get(path);
    expect(response.status(), path).toBe(401);
  }
});

test("shows digest list and a safe detail view with original source links", async ({ browser }) => {
  const { context, page } = await authenticatedPage(browser);
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
  await context.close();
});

test("does not make an untrusted post source URL clickable", async ({ browser }) => {
  const { context, page } = await authenticatedPage(browser);
  await page.goto("/posts?author=unsafe-link");

  await expect(page.getByText("Original launch notes with practical deployment details.")).toBeVisible();
  await expect(page.getByRole("link", { name: "View original on X" })).toHaveCount(0);
  await context.close();
});

test("does not make an untrusted digest snapshot URL clickable", async ({ browser }) => {
  const { context, page } = await authenticatedPage(browser);
  await page.goto("/digests/digest-unsafe");

  await expect(page.getByRole("heading", { name: "Unsafe Digest" })).toBeVisible();
  await expect(page.getByRole("link", { name: "View original on X" })).toHaveCount(0);
  await context.close();
});

test("keeps every post filter in the URL and shows the matching stream", async ({ browser }) => {
  const { context, page } = await authenticatedPage(browser);
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
  await context.close();
});

test("includes the whole selected end date in the backend filter", async ({ browser }) => {
  const { context, page } = await authenticatedPage(browser);
  await page.goto("/posts?author=date-boundary&to=2026-09-11");

  await expect(page).toHaveURL(/to=2026-09-11/);
  await expect(page.getByText("A post from the final minute of the selected day.")).toBeVisible();
  await context.close();
});

test("loads and appends the next post cursor page", async ({ browser }) => {
  const { context, page } = await authenticatedPage(browser);
  await page.goto("/posts?author=pagination");

  await expect(page.getByText("First page post")).toBeVisible();
  await page.getByRole("button", { name: "Load more posts" }).click();
  await expect(page.getByText("First page post")).toBeVisible();
  await expect(page.getByText("Second page post")).toBeVisible();
  await expect(page.getByRole("button", { name: "Load more posts" })).toHaveCount(0);
  await context.close();
});

test("updates post state optimistically and regenerates its summary", async ({ browser }) => {
  const { context, page } = await authenticatedPage(browser);
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
  await context.close();
});

test("rolls back an optimistic action and announces the failure", async ({ browser }) => {
  const { context, page } = await authenticatedPage(browser);
  await page.goto("/posts?author=fail-actions");
  const save = page.getByRole("button", { name: "Save post" });

  await save.click();
  await expect(save).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator(".error-message[role=alert]")).toContainText("Could not update this post");
  await context.close();
});

test("regenerates a digest and shows the new immutable version", async ({ browser }) => {
  const { context, page } = await authenticatedPage(browser);
  await page.goto("/digests/digest-1");

  await page.getByRole("button", { name: "Regenerate digest" }).click();
  await expect(page).toHaveURL(/\/digests\/digest-2$/);
  await expect(page.getByText("Version 2")).toBeVisible();
  await expect(page.getByText("A newly sourced post for digest version two.")).toBeVisible();
  await expect(page.getByRole("link", { name: "View original on X" })).toHaveAttribute(
    "href",
    "https://twitter.com/bob/status/19002",
  );
  await context.close();
});

test("shows loading, empty, and backend error states", async ({ browser }) => {
  const { context, page } = await authenticatedPage(browser);
  await page.goto("/posts?author=loading");
  await expect(page.getByText("Loading posts…")).toBeVisible();
  await expect(page.getByText("Original launch notes with practical deployment details.")).toBeVisible();

  await page.goto("/posts?author=empty");
  await expect(page.getByText("No posts match these filters.")).toBeVisible();

  await page.goto("/posts?author=error");
  await expect(page.locator(".error-message[role=alert]")).toContainText("Could not load posts");
  await context.close();
});
