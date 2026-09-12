import { NextResponse } from "next/server";

import {
  createOAuthState,
  OAUTH_STATE_COOKIE,
  OAUTH_STATE_MAX_AGE,
  validStateSecret,
} from "../../../../lib/oauth-state";

const DEFAULT_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize";
const OAUTH_SCOPE = "tweet.read users.read list.read offline.access";

function configuredUrl(value: string | undefined): URL | null {
  try {
    const url = new URL(value ?? "");
    return url.protocol === "http:" || url.protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

function unavailable(): NextResponse {
  return NextResponse.json(
    { code: "oauth_not_configured", message: "X OAuth is not configured" },
    { status: 503 },
  );
}

export async function GET(): Promise<NextResponse> {
  const clientId = process.env.X_CLIENT_ID?.trim();
  const redirectUri = configuredUrl(process.env.X_OAUTH_REDIRECT_URI);
  const stateSecret = process.env.X_OAUTH_STATE_SECRET;
  const authorizeUrl = configuredUrl(process.env.X_OAUTH_AUTHORIZE_URL ?? DEFAULT_AUTHORIZE_URL);
  if (!clientId || !redirectUri || !validStateSecret(stateSecret) || !authorizeUrl) {
    return unavailable();
  }

  const oauthState = await createOAuthState(stateSecret);
  authorizeUrl.search = "";
  authorizeUrl.searchParams.set("client_id", clientId);
  authorizeUrl.searchParams.set("redirect_uri", redirectUri.toString());
  authorizeUrl.searchParams.set("response_type", "code");
  authorizeUrl.searchParams.set("scope", OAUTH_SCOPE);
  authorizeUrl.searchParams.set("state", oauthState.payload.state);
  authorizeUrl.searchParams.set("code_challenge", oauthState.challenge);
  authorizeUrl.searchParams.set("code_challenge_method", "S256");

  const response = NextResponse.redirect(authorizeUrl, 307);
  response.cookies.set(OAUTH_STATE_COOKIE, oauthState.cookie, {
    httpOnly: true,
    sameSite: "lax",
    path: "/api/x",
    maxAge: OAUTH_STATE_MAX_AGE,
    secure: process.env.NODE_ENV === "production",
  });
  return response;
}
