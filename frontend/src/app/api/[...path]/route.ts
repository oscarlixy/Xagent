import type { NextRequest } from "next/server";

const ALLOWED_REQUEST_HEADERS = ["accept", "content-type", "idempotency-key"] as const;
const ALLOWED_RESPONSE_HEADERS = ["content-type", "x-request-id"] as const;
const TIMEOUT_MS = 8_000;

type RouteContext = { params: Promise<{ path: string[] }> };

async function proxy(request: NextRequest, context: RouteContext): Promise<Response> {
  const backendUrl = process.env.BACKEND_INTERNAL_URL;
  const internalToken = process.env.INTERNAL_API_TOKEN;
  if (!backendUrl || !internalToken) {
    return Response.json(
      { code: "bff_not_configured", message: "Backend connection is not configured" },
      { status: 503 },
    );
  }

  const { path } = await context.params;
  const target = new URL(`/api/${path.map(encodeURIComponent).join("/")}`, backendUrl);
  target.search = request.nextUrl.search;

  const headers = new Headers({ Authorization: `Bearer ${internalToken}` });
  for (const name of ALLOWED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === "GET" ? undefined : await request.arrayBuffer(),
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    const responseHeaders = new Headers();
    for (const name of ALLOWED_RESPONSE_HEADERS) {
      const value = upstream.headers.get(name);
      if (value !== null) responseHeaders.set(name, value);
    }
    return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
    return Response.json(
      {
        code: timedOut ? "backend_timeout" : "backend_unavailable",
        message: timedOut ? "The backend request timed out" : "The backend is unavailable",
      },
      { status: timedOut ? 504 : 502 },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
