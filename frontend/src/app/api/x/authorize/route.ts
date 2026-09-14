import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

import {
  createOAuthState,
  OAUTH_STATE_COOKIE,
  OAUTH_STATE_MAX_AGE,
} from "../../../../lib/oauth-state";
import {
  configuredHttpUrlForProvider,
  oauthConfiguration,
  requestOrigin,
} from "../../../../lib/oauth-config";

const DEFAULT_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize";
const OAUTH_SCOPE = "tweet.read users.read list.read offline.access";

function unavailable(): NextResponse {
  return NextResponse.json(
    { code: "oauth_not_configured", message: "X OAuth is not configured" },
    { status: 503 },
  );
}

export async function GET(request: NextRequest): Promise<NextResponse> {
  const origin = requestOrigin(request.nextUrl.protocol, request.headers.get("host"));
  const config = origin ? oauthConfiguration(origin) : null;
  const authorizeUrl = configuredHttpUrlForProvider(process.env.X_OAUTH_AUTHORIZE_URL ?? DEFAULT_AUTHORIZE_URL);
  if (!config || !authorizeUrl) {
    return unavailable();
  }

  const oauthState = await createOAuthState(config.stateSecret);
  authorizeUrl.search = "";
  authorizeUrl.searchParams.set("client_id", config.clientId);
  authorizeUrl.searchParams.set("redirect_uri", config.redirectUri.toString());
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
