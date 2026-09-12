const TRUSTED_SOURCE_HOSTS = ["x.com", "twitter.com"] as const;

export function trustedSourceUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    const hostname = url.hostname.toLowerCase();
    const trustedHost = TRUSTED_SOURCE_HOSTS.some(
      (host) => hostname === host || hostname.endsWith(`.${host}`),
    );
    if (
      url.protocol !== "https:" ||
      !trustedHost ||
      url.username !== "" ||
      url.password !== "" ||
      (url.port !== "" && url.port !== "443")
    ) {
      return null;
    }
    return url.toString();
  } catch {
    return null;
  }
}
