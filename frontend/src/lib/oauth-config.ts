import { validStateSecret } from "./oauth-state";

const CALLBACK_PATH = "/api/x/callback";
const encoder = new TextEncoder();

export type OAuthConfiguration = {
  backendUrl: URL;
  clientId: string;
  internalToken: string;
  redirectUri: URL;
  stateSecret: string;
};

function configuredHttpUrl(value: string | undefined): URL | null {
  try {
    const url = new URL(value ?? "");
    return url.protocol === "http:" || url.protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

function validCallbackUri(redirectUri: URL, requestOrigin: string): boolean {
  return (
    redirectUri.origin === requestOrigin &&
    redirectUri.pathname === CALLBACK_PATH &&
    !redirectUri.search &&
    !redirectUri.hash &&
    !redirectUri.username &&
    !redirectUri.password
  );
}

function validBackendUrl(backendUrl: URL): boolean {
  return !backendUrl.search && !backendUrl.hash && !backendUrl.username && !backendUrl.password;
}

export function requestOrigin(protocol: string, host: string | null): string | null {
  try {
    const url = new URL(`${protocol}//${host ?? ""}`);
    if (
      (url.protocol !== "http:" && url.protocol !== "https:") ||
      url.pathname !== "/" ||
      url.search ||
      url.hash ||
      url.username ||
      url.password
    ) {
      return null;
    }
    return url.origin;
  } catch {
    return null;
  }
}

export function oauthConfiguration(requestOrigin: string): OAuthConfiguration | null {
  const clientId = process.env.X_CLIENT_ID?.trim();
  const redirectUri = configuredHttpUrl(process.env.X_OAUTH_REDIRECT_URI);
  const stateSecret = process.env.X_OAUTH_STATE_SECRET;
  const backendUrl = configuredHttpUrl(process.env.BACKEND_INTERNAL_URL);
  const internalToken = process.env.INTERNAL_API_TOKEN;
  if (
    !clientId ||
    !redirectUri ||
    !validCallbackUri(redirectUri, requestOrigin) ||
    !validStateSecret(stateSecret) ||
    !backendUrl ||
    !validBackendUrl(backendUrl) ||
    !internalToken ||
    encoder.encode(internalToken).byteLength < 32
  ) {
    return null;
  }
  return { backendUrl, clientId, internalToken, redirectUri, stateSecret };
}

export function configuredHttpUrlForProvider(value: string | undefined): URL | null {
  return configuredHttpUrl(value);
}
