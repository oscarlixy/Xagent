const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });

export const OAUTH_STATE_COOKIE = "x_oauth_state";
export const OAUTH_STATE_MAX_AGE = 600;

export type OAuthState = {
  state: string;
  verifier: string;
  expiry: number;
};

function encodeBase64Url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/u, "");
}

function decodeBase64Url(value: string): ArrayBuffer {
  if (!/^[A-Za-z0-9_-]+$/u.test(value)) throw new Error("Invalid base64url");
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0)).buffer;
}

function randomBase64Url(): string {
  return encodeBase64Url(crypto.getRandomValues(new Uint8Array(32)));
}

async function stateKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

export function validStateSecret(secret: string | undefined): secret is string {
  return typeof secret === "string" && encoder.encode(secret).byteLength >= 32;
}

export async function createOAuthState(secret: string): Promise<{
  cookie: string;
  payload: OAuthState;
  challenge: string;
}> {
  const verifier = randomBase64Url();
  const payload: OAuthState = {
    state: randomBase64Url(),
    verifier,
    expiry: Math.floor(Date.now() / 1000) + OAUTH_STATE_MAX_AGE,
  };
  const encodedPayload = encodeBase64Url(encoder.encode(JSON.stringify(payload)));
  const signature = await crypto.subtle.sign("HMAC", await stateKey(secret), encoder.encode(encodedPayload));
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(verifier));
  return {
    cookie: `${encodedPayload}.${encodeBase64Url(new Uint8Array(signature))}`,
    payload,
    challenge: encodeBase64Url(new Uint8Array(digest)),
  };
}

export async function verifyOAuthState(cookie: string | undefined, secret: string): Promise<OAuthState | null> {
  if (!cookie || cookie.length > 4096) return null;
  const parts = cookie.split(".");
  if (parts.length !== 2) return null;
  const [encodedPayload, encodedSignature] = parts;

  try {
    const validSignature = await crypto.subtle.verify(
      "HMAC",
      await stateKey(secret),
      decodeBase64Url(encodedSignature),
      encoder.encode(encodedPayload),
    );
    if (!validSignature) return null;

    const payload = JSON.parse(decoder.decode(decodeBase64Url(encodedPayload))) as Partial<OAuthState>;
    if (
      typeof payload.state !== "string" ||
      payload.state.length === 0 ||
      typeof payload.verifier !== "string" ||
      payload.verifier.length === 0 ||
      !Number.isInteger(payload.expiry) ||
      (payload.expiry as number) <= Math.floor(Date.now() / 1000)
    ) {
      return null;
    }
    return payload as OAuthState;
  } catch {
    return null;
  }
}
