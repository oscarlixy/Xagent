import { NextRequest, NextResponse } from "next/server";

const CHALLENGE = { "WWW-Authenticate": 'Basic realm="X Digest"' };

function safeEqual(actual: string, expected: string): boolean {
  const length = Math.max(actual.length, expected.length);
  let difference = actual.length ^ expected.length;
  for (let index = 0; index < length; index += 1) {
    difference |= (actual.charCodeAt(index) || 0) ^ (expected.charCodeAt(index) || 0);
  }
  return difference === 0;
}

export function middleware(request: NextRequest) {
  if (request.nextUrl.pathname === "/health") return NextResponse.next();

  const username = process.env.OPERATOR_USERNAME;
  const password = process.env.OPERATOR_PASSWORD;
  if (!username || !password) {
    return NextResponse.json(
      { code: "operator_credentials_missing", message: "Operator credentials are not configured" },
      { status: 503 },
    );
  }

  const authorization = request.headers.get("authorization");
  if (authorization?.startsWith("Basic ")) {
    try {
      const decoded = atob(authorization.slice(6));
      const separator = decoded.indexOf(":");
      const suppliedUsername = decoded.slice(0, separator);
      const suppliedPassword = decoded.slice(separator + 1);
      if (
        separator >= 0 &&
        safeEqual(suppliedUsername, username) &&
        safeEqual(suppliedPassword, password)
      ) {
        return NextResponse.next();
      }
    } catch {
      // Invalid base64 is treated as an authentication failure.
    }
  }

  return new NextResponse("Authentication required", { status: 401, headers: CHALLENGE });
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico$).*)"],
};
