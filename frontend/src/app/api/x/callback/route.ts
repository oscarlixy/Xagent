import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

import { OAUTH_STATE_COOKIE, validStateSecret, verifyOAuthState } from "../../../../lib/oauth-state";

const TIMEOUT_MS = 8_000;

type CallbackConfig = {
  backendUrl: URL;
  internalToken: string;
  redirectUri: URL;
  stateSecret: string;
};

function configuredUrl(value: string | undefined): URL | null {
  try {
    const url = new URL(value ?? "");
    return url.protocol === "http:" || url.protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

function configuration(): CallbackConfig | null {
  const clientId = process.env.X_CLIENT_ID?.trim();
  const redirectUri = configuredUrl(process.env.X_OAUTH_REDIRECT_URI);
  const stateSecret = process.env.X_OAUTH_STATE_SECRET;
  const backendUrl = configuredUrl(process.env.BACKEND_INTERNAL_URL);
  const internalToken = process.env.INTERNAL_API_TOKEN;
  if (
    !clientId ||
    !redirectUri ||
    !validStateSecret(stateSecret) ||
    !backendUrl ||
    !internalToken ||
    new TextEncoder().encode(internalToken).byteLength < 32
  ) {
    return null;
  }
  return { backendUrl, internalToken, redirectUri, stateSecret };
}

function clearState(response: NextResponse): NextResponse {
  response.cookies.set(OAUTH_STATE_COOKIE, "", {
    httpOnly: true,
    sameSite: "lax",
    path: "/api/x",
    maxAge: 0,
    secure: process.env.NODE_ENV === "production",
  });
  return response;
}

function unavailable(): NextResponse {
  return clearState(
    NextResponse.json(
      { code: "oauth_not_configured", message: "X OAuth is not configured" },
      { status: 503 },
    ),
  );
}

function redirect(config: CallbackConfig, outcome: "success" | "failed" | "denied"): NextResponse {
  const target = new URL("/", config.redirectUri);
  if (outcome !== "success") target.searchParams.set("x_auth", outcome);
  return clearState(NextResponse.redirect(target, 307));
}

export async function GET(request: NextRequest): Promise<NextResponse> {
  const config = configuration();
  if (!config) return unavailable();

  const suppliedState = request.nextUrl.searchParams.get("state");
  const storedState = await verifyOAuthState(request.cookies.get(OAUTH_STATE_COOKIE)?.value, config.stateSecret);
  if (!storedState || !suppliedState || suppliedState !== storedState.state) {
    return redirect(config, "failed");
  }

  if (request.nextUrl.searchParams.get("error") === "access_denied") {
    return redirect(config, "denied");
  }

  const code = request.nextUrl.searchParams.get("code");
  if (!code) return redirect(config, "failed");

  const target = new URL("/api/x/oauth/callback", config.backendUrl);
  try {
    const upstream = await fetch(target, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${config.internalToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ code, verifier: storedState.verifier }),
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    return redirect(config, upstream.status === 204 ? "success" : "failed");
  } catch {
    return redirect(config, "failed");
  }
}
