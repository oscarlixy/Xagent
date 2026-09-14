import { expect, test } from "@playwright/test";
import { NextRequest } from "next/server";

import { GET as authorize } from "../src/app/api/x/authorize/route";
import { GET as callback } from "../src/app/api/x/callback/route";
import * as oauthState from "../src/lib/oauth-state";

const requiredEnvironment = {
  BACKEND_INTERNAL_URL: "http://backend.test:8000",
  INTERNAL_API_TOKEN: "test-internal-token-that-is-at-least-32-bytes",
  X_CLIENT_ID: "test-x-client-id",
  X_OAUTH_REDIRECT_URI: "http://reader.test/api/x/callback",
  X_OAUTH_STATE_SECRET: "test-x-oauth-state-secret-32-bytes-minimum",
  X_OAUTH_AUTHORIZE_URL: "http://provider.test/authorize",
};

async function withOAuthEnvironment(
  overrides: Partial<Record<keyof typeof requiredEnvironment, string | undefined>>,
  action: () => Promise<void>,
) {
  const values = { ...requiredEnvironment, ...overrides };
  const original = new Map<string, string | undefined>();
  for (const [key, value] of Object.entries(values)) {
    original.set(key, process.env[key]);
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }

  try {
    await action();
  } finally {
    for (const [key, value] of original) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

test("compares callback state through a constant-time byte comparison", () => {
  const compare = (oauthState as unknown as {
    constantTimeEqual?: (actual: string, expected: string) => boolean;
  }).constantTimeEqual;

  expect(compare?.("matching-state", "matching-state")).toBe(true);
  expect(compare?.("matching-state", "mismatch-state")).toBe(false);
});

test("requires the redirect URI origin to match the request host", async () => {
  await withOAuthEnvironment({}, async () => {
    const response = await authorize(
      new NextRequest("http://reader.test/api/x/authorize", { headers: { host: "elsewhere.test" } }),
    );

    expect(response.status).toBe(503);
    expect(response.headers.get("location")).toBeNull();
  });
});

for (const [name, overrides] of [
  ["missing backend URL", { BACKEND_INTERNAL_URL: undefined }],
  ["invalid backend URL", { BACKEND_INTERNAL_URL: "ftp://backend.test" }],
  ["short internal token", { INTERNAL_API_TOKEN: "too-short" }],
] as const) {
  test(`does not start OAuth when callback prerequisites have ${name}`, async () => {
  await withOAuthEnvironment(overrides, async () => {
    const response = await authorize(new NextRequest("http://reader.test/api/x/authorize"));

    expect(response.status).toBe(503);
    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.get("set-cookie")).toBeNull();
  });
  });
}

for (const [name, redirectUri] of [
  ["a query", "http://reader.test/api/x/callback?unexpected=query"],
  ["a fragment", "http://reader.test/api/x/callback#unexpected-fragment"],
  ["credentials", "http://operator:password@reader.test/api/x/callback"],
  ["a non-callback path", "http://reader.test/api/x/callback/"],
  ["a mismatched request origin", "http://elsewhere.test/api/x/callback"],
] as const) {
  test(`rejects redirect URI with ${name} before the authorization redirect`, async () => {
  await withOAuthEnvironment({ X_OAUTH_REDIRECT_URI: redirectUri }, async () => {
    const authorizeResponse = await authorize(new NextRequest("http://reader.test/api/x/authorize"));
    expect(authorizeResponse.status).toBe(503);
    expect(authorizeResponse.headers.get("location")).toBeNull();
    expect(authorizeResponse.headers.get("set-cookie")).toBeNull();

    const callbackResponse = await callback(
      new NextRequest("http://reader.test/api/x/callback?code=must-not-leak&state=must-not-leak"),
    );
    expect(callbackResponse.status).toBe(503);
    expect(await callbackResponse.text()).not.toContain("must-not-leak");
  });
  });
}
